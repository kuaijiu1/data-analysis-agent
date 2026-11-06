"""Start/stop a temporary HTTPS tunnel without changing the local server binding."""

import argparse
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BINARY = DATA / "tools" / "cloudflared.exe"
STATE = DATA / "public_tunnel.json"
ADDRESS = DATA / "public_url.txt"
ERROR_LOG = DATA / "public-error.log"
VERSION = "2026.10.7"
SHA256 = "2493dbc168923bb3e59e5e7716f71deced92992eea9acd6e9d74f84ac7751343"
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@contextmanager
def process_handle(pid, terminate=False):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000 | (0x0001 if terminate else 0), False, int(pid))
    try:
        yield kernel, handle
    finally:
        if handle:
            kernel.CloseHandle(handle)


def process_info(kernel, handle):
    if not handle:
        return None
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    size = wintypes.DWORD(32768)
    name = ctypes.create_unicode_buffer(size.value)
    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
        return None
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        return None
    created = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 10_000_000 - 11644473600
    return {"executable": name.value, "created": created}


def matches_process(state, info):
    """Never trust a reused PID or a process outside this project's binary path."""
    if not info:
        return False
    expected_time = state.get("process_created", state.get("started_at", 0))
    return (os.path.normcase(str(Path(info["executable"]).resolve())) == os.path.normcase(str(BINARY.resolve()))
            and abs(info["created"] - expected_time) < 2)


def is_running(state):
    with process_handle(state.get("pid", 0)) as (kernel, handle):
        return matches_process(state, process_info(kernel, handle))


def terminate(state):
    # Check image path and creation time on the same handle used to terminate.
    with process_handle(state.get("pid", 0), terminate=True) as (kernel, handle):
        if not matches_process(state, process_info(kernel, handle)):
            return False
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        if not kernel.TerminateProcess(handle, 0):
            raise OSError("无法关闭外网通道，请检查该程序是否仍在运行。")
        return True


def read_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ready(state):
    if not state.get("metrics_port"):
        return False
    try:
        with DIRECT.open(f"http://127.0.0.1:{int(state['metrics_port'])}/ready", timeout=2) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def save_state(state):
    temporary = STATE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(STATE)


def install_binary():
    def verified(path):
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest() == SHA256

    if BINARY.exists() and verified(BINARY):
        return
    BINARY.parent.mkdir(parents=True, exist_ok=True)
    print("正在从 Cloudflare 官方发布页下载外网工具…", flush=True)
    download = BINARY.with_suffix(".download")
    url = f"https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/cloudflared-windows-amd64.exe"
    with urllib.request.urlopen(url, timeout=30) as source, download.open("wb") as output:
        while block := source.read(1024 * 1024):
            output.write(block)
    if not verified(download):
        raise RuntimeError("下载文件校验未通过，已停止启动。")
    download.replace(BINARY)


@contextmanager
def launch_lock():
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "public-startup.lock").open("a+b") as lock:
        if lock.seek(0, os.SEEK_END) == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("另一个外网启动/关闭操作正在进行，请稍后重试。") from exc
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def start():
    from start_local import start_server

    start_server()
    state = read_state()
    if is_running(state):
        if ready(state) and state.get("url"):
            ADDRESS.write_text(state["url"] + "\n", encoding="utf-8")
            return state["url"]
        raise RuntimeError("外网通道正在连接或重连，请稍后再试；也可先关闭外网访问再重新开启。")
    install_binary()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        metrics_port = probe.getsockname()[1]
    with (DATA / "public-out.log").open("wb") as output, ERROR_LOG.open("wb") as error:
        child = subprocess.Popen(
            [str(BINARY), "tunnel", "--no-autoupdate", "--protocol", "auto", "--metrics",
             f"127.0.0.1:{metrics_port}", "--url", "http://127.0.0.1:8000"],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=error,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    state = {"pid": child.pid, "executable": str(BINARY), "metrics_port": metrics_port, "started_at": time.time(), "url": None}
    with process_handle(child.pid) as (kernel, handle):
        info = process_info(kernel, handle)
        if info:
            state["process_created"] = info["created"]
    save_state(state)
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError("外网通道启动失败，请查看 data/public-error.log。")
            log = ERROR_LOG.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", log)
            if match:
                state["url"] = match.group()
                save_state(state)
                if ready(state):
                    ADDRESS.write_text(state["url"] + "\n", encoding="utf-8")
                    return state["url"]
            time.sleep(.4)
        raise RuntimeError("外网通道连接超时，请检查网络并查看 data/public-error.log。")
    except Exception:
        terminate(state)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"], default="start", nargs="?")
    args = parser.parse_args()
    try:
        with launch_lock():
            if args.action == "start":
                url = start()
                print(f"\n外网访问地址：\n{url}\n\n把上面的完整链接发给对方即可。对方需注册/登录自己的账号。")
                print("电脑、网站和外网通道需保持运行；关闭本窗口不影响通道。")
                print("重启通道后链接可能变化。当前地址也保存在 data/public_url.txt。")
            elif args.action == "stop":
                stopped = terminate(read_state())
                STATE.unlink(missing_ok=True)
                ADDRESS.write_text("外网访问已关闭。重新开启请运行 start_public.cmd。\n", encoding="utf-8")
                print("外网访问已关闭，本机网站仍可使用。" if stopped else "未找到此项目正在运行的外网通道，本机网站仍可使用。")
            else:
                state = read_state()
                print(state.get("url") if is_running(state) and ready(state) else "外网访问未就绪，请运行 start_public.cmd。")
        return 0
    except Exception as exc:
        print(f"操作未完成：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
