"""Start the local web server once, independently of a terminal window."""

import argparse
import json
import msvcrt
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "data"
URL = "http://localhost:8000/"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def healthy():
    try:
        with OPENER.open("http://127.0.0.1:8000/api/auth/status", timeout=1) as response:
            status = json.load(response)
        return isinstance(status, dict) and {
            "authenticated", "email", "legacy_available"
        }.issubset(status)
    except (OSError, ValueError):
        return False


def start_server():
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        raise RuntimeError("Missing .venv/Scripts/python.exe. Follow README.md to install Python dependencies.")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Windows releases this lock even if a launcher is terminated unexpectedly.
    with (LOG_DIR / "startup.lock").open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        deadline = time.monotonic() + 40
        while True:
            try:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another launcher is still starting the server. Try again shortly.")
                time.sleep(0.25)
        try:
            if healthy():
                return
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", 8000)) == 0:
                    raise RuntimeError("Port 8000 is occupied, but the app is not responding. See data/server-error.log.")
            with (LOG_DIR / "server-out.log").open("ab") as output, (LOG_DIR / "server-error.log").open("ab") as error:
                child = subprocess.Popen(
                    [str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=error,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    env={**os.environ, "PYTHONUTF8": "1"},
                )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if healthy():
                    return
                if child.poll() is not None:
                    raise RuntimeError("Server exited during startup. See data/server-error.log.")
                time.sleep(0.3)
            raise RuntimeError("Server startup timed out. See data/server-error.log; try run.cmd again shortly.")
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    try:
        start_server()
        if sys.stdout:
            print(f"Website is ready: {URL}")
        if args.open_browser:
            webbrowser.open(URL)
        return 0
    except Exception as exc:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        message = f"{time.strftime('%Y-%m-%d %H:%M:%S')} Startup failed: {exc}\n"
        with (LOG_DIR / "startup-error.log").open("a", encoding="utf-8") as log:
            log.write(message)
        if sys.stderr:
            print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
