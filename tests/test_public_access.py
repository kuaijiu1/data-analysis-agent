"""Tunnel lifecycle checks use disposable subprocesses, never the live tunnel."""

import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import public_access


class PublicAccessTests(unittest.TestCase):
    def child(self):
        process = subprocess.Popen([sys._base_executable, "-c", "import time; time.sleep(30)"], creationflags=subprocess.CREATE_NO_WINDOW)
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        self.addCleanup(cleanup)
        with public_access.process_handle(process.pid) as (kernel, handle):
            info = public_access.process_info(kernel, handle)
        self.assertIsNotNone(info)
        return process, info

    def test_stop_refuses_unrelated_executable_and_reused_pid(self):
        child, info = self.child()
        state = {"pid": child.pid, "process_created": info["created"]}
        self.assertFalse(public_access.terminate(state))
        self.assertIsNone(child.poll())
        with patch.object(public_access, "BINARY", Path(info["executable"])):
            state["process_created"] -= 100
            self.assertFalse(public_access.terminate(state))
            self.assertIsNone(child.poll())

    def test_stop_terminates_only_matching_disposable_process(self):
        child, info = self.child()
        state = {"pid": child.pid, "process_created": info["created"]}
        with patch.object(public_access, "BINARY", Path(info["executable"])):
            self.assertTrue(public_access.is_running(state))
            self.assertTrue(public_access.terminate(state))
        self.assertEqual(child.wait(timeout=5), 0)

    def test_download_checksum_failure_does_not_install_binary(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "cloudflared.exe"
            with patch.object(public_access, "BINARY", binary), patch.object(public_access.urllib.request, "urlopen", return_value=io.BytesIO(b"tampered download")):
                with self.assertRaisesRegex(RuntimeError, "校验未通过"):
                    public_access.install_binary()
            self.assertFalse(binary.exists())

    def test_existing_ready_tunnel_is_reused_without_launching_another(self):
        with tempfile.TemporaryDirectory() as folder:
            address = Path(folder) / "address.txt"
            state = {"pid": 123, "url": "https://test-example.trycloudflare.com"}
            with patch("start_local.start_server"), patch.object(public_access, "read_state", return_value=state), patch.object(public_access, "is_running", return_value=True), patch.object(public_access, "ready", return_value=True), patch.object(public_access, "ADDRESS", address), patch.object(public_access.subprocess, "Popen") as launch:
                self.assertEqual(public_access.start(), state["url"])
                launch.assert_not_called()
            self.assertEqual(address.read_text(encoding="utf-8").strip(), state["url"])
