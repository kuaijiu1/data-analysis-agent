"""Repeatable localhost upload/import benchmark using disposable synthetic data."""

import argparse
import ctypes
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import duckdb
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]


def log(message):
    print(message, flush=True)


def csv_file(path, size):
    header = b"region,sales,orders,payload\n"
    prefix = b"East,100.50,2,"
    row_size = 512
    lines = [prefix + (hashlib.sha256(str(i).encode()).hexdigest().encode() * 8)[:row_size - len(prefix) - 1] + b"\n" for i in range(4096)]
    count, remainder = divmod(size - len(header), row_size)
    if remainder and remainder < len(prefix) + 1:
        count -= 1; remainder += row_size
    with path.open("wb") as output:
        output.write(header)
        block = b"".join(lines)
        blocks, tail = divmod(count, len(lines))
        for _ in range(blocks):
            output.write(block)
        output.write(b"".join(lines[:tail]))
        if remainder:
            output.write(prefix + b"x" * (remainder - len(prefix) - 1) + b"\n")
    assert path.stat().st_size == size
    return count + bool(remainder)


def excel_file(path, rows):
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("sales")
    sheet.append(["region", "sales", "orders", "payload"])
    for i in range(rows):
        sheet.append(["East" if i % 2 else "West", 100.5, 2, hashlib.sha256(str(i).encode()).hexdigest()])
    workbook.save(path)
    return rows


def peak_memory(pid):
    if os.name != "nt":
        return None
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in ["peak", "working", "paged_peak", "paged", "nonpaged_peak", "nonpaged", "pagefile", "peak_pagefile"]]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x0410, False, pid)
    if not handle:
        return None
    counters = Counters(); counters.cb = ctypes.sizeof(counters)
    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None
        return counters.peak
    finally:
        kernel.CloseHandle(handle)


def benchmark(source, expected_rows, folder):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
    environment = dict(os.environ, DATA_AGENT_DATA_DIR=str(folder / "storage"))
    log_path = folder / "server.log"
    process = None
    with log_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                                   cwd=ROOT, env=environment, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        def request(path, method="GET", payload=None, raw=None):
            data = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
            headers = {"Content-Type": "application/json"} if payload is not None else {}
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
            with opener.open(req, timeout=120) as response:
                return json.load(response)
        server_pid = None
        try:
            for _ in range(100):
                try:
                    request("/api/auth/status"); break
                except Exception:
                    if process.poll() is not None:
                        raise RuntimeError(log_path.read_text(encoding="utf-8"))
                    time.sleep(.1)
            else:
                raise RuntimeError("Benchmark server did not start")
            match = re.search(r"Started server process \[(\d+)\]", log_path.read_text(encoding="utf-8"))
            server_pid = int(match.group(1)) if match else process.pid
            request("/api/auth/register", "POST", {"email": "benchmark@example.invalid", "password": "benchmark-only-password"})
            size = source.stat().st_size
            started = time.perf_counter()
            item = request("/api/uploads", "POST", {"name": source.name, "size": size})
            item_id = item["id"]; offset = 0
            with source.open("rb") as handle:
                while block := handle.read(8 * 1024 ** 2):
                    response = request(f"/api/uploads/{item_id}/chunk?offset={offset}", "PUT", raw=block)
                    offset = response["received"]
                    if offset % (256 * 1024 ** 2) == 0:
                        log(f"{source.suffix}: uploaded {offset // 1024 ** 2} MiB")
            upload_seconds = time.perf_counter() - started
            started = time.perf_counter()
            request(f"/api/uploads/{item_id}/complete", "POST")
            last_log = 0
            while time.perf_counter() - started < 1200:
                item = request(f"/api/datasets/{item_id}")
                if item["status"] == "awaiting_sheets":
                    request(f"/api/datasets/{item_id}/import", "POST", {"sheets": [item["sheets"][0]]})
                elif item["status"] == "ready":
                    break
                elif item["status"] in {"error", "cancelled", "interrupted"}:
                    raise RuntimeError(item)
                if time.perf_counter() - last_log > 10:
                    log(f"{source.suffix}: {item['progress']}"); last_log = time.perf_counter()
                time.sleep(.2)
            else:
                raise RuntimeError("Import exceeded 20 minutes")
            import_seconds = time.perf_counter() - started
            assert item["tables"][0]["rows"] == expected_rows, item["tables"][0]["rows"]
            db_path = folder / "storage" / "databases" / f"{item_id}.duckdb"
            started = time.perf_counter()
            with duckdb.connect(str(db_path), read_only=True) as connection:
                actual_rows, total = connection.execute(f'SELECT COUNT(*), SUM(sales) FROM "{item["tables"][0]["name"]}"').fetchone()
            query_seconds = time.perf_counter() - started
            assert actual_rows == expected_rows and abs(float(total) - expected_rows * 100.5) < .01
            result = {"format": source.suffix, "file_bytes": size, "rows": expected_rows,
                      "upload_seconds": round(upload_seconds, 3), "import_and_profile_seconds": round(import_seconds, 3),
                      "aggregate_query_seconds": round(query_seconds, 3), "peak_server_working_set_bytes": peak_memory(server_pid),
                      "persistent_storage_bytes": item["storage_bytes"], "profile_status": item.get("profile_status"),
                      "row_count_and_sum_verified": True}
            log(json.dumps(result, ensure_ascii=False))
            for _ in range(30):
                try:
                    request(f"/api/datasets/{item_id}", "DELETE"); break
                except urllib.error.HTTPError as exc:
                    if exc.code != 409:
                        raise
                    time.sleep(.1)
            else:
                raise RuntimeError("Could not clean benchmark dataset")
            return result
        finally:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                process.terminate()
            process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv-mib", type=int, default=1024)
    parser.add_argument("--excel-rows", type=int, default=100000)
    args = parser.parse_args()
    if not 1 <= args.csv_mib <= 1024 or not 1 <= args.excel_rows <= 1_000_000:
        parser.error("CSV size must be 1..1024 MiB, Excel rows 1..1000000")
    data_root = ROOT / "data"; data_root.mkdir(exist_ok=True)
    if shutil.disk_usage(data_root).free < args.csv_mib * 1024 ** 2 * 4 + 512 * 1024 ** 2:
        raise RuntimeError("Not enough disk space for benchmark")
    temporary = tempfile.TemporaryDirectory(prefix="benchmark-", dir=data_root)
    folder = Path(temporary.name).resolve()
    if folder == data_root.resolve() or not folder.is_relative_to(data_root.resolve()):
        raise RuntimeError("Unsafe benchmark folder")
    try:
        source = folder / "synthetic.csv"
        rows = csv_file(source, args.csv_mib * 1024 ** 2)
        log(f"Generated CSV: {source.stat().st_size} bytes, {rows} rows")
        results = [benchmark(source, rows, folder)]
        source.unlink()
        source = folder / "synthetic.xlsx"
        rows = excel_file(source, args.excel_rows)
        log(f"Generated XLSX: {source.stat().st_size} bytes, {rows} rows")
        # Each case runs in a fresh server process for independent peak memory measurements.
        store_folder = (folder / "storage").resolve()
        if store_folder == folder or not store_folder.is_relative_to(folder):
            raise RuntimeError("Unsafe benchmark storage folder")
        shutil.rmtree(store_folder)
        results.append(benchmark(source, rows, folder))
        report = {"measured_at": time.strftime("%Y-%m-%d %H:%M:%S"), "python": sys.version,
                  "method": "Actual localhost HTTP requests, 8 MiB chunks, fresh Uvicorn per format, no LLM calls. CSV has repeated synthetic 512-byte rows and is highly compressible. XLSX is a representative-sized fixture, not a 1 GiB XLSX test.",
                  "results": results}
        target = ROOT / "benchmarks" / "latest.json"; target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"Report: {target}")
    finally:
        # The resolved path above was checked to be a unique child of data/.
        temporary.cleanup()


if __name__ == "__main__":
    main()
