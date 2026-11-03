import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RecoveryTests(unittest.TestCase):
    def test_restart_discards_uncommitted_chunk_tail_and_recovers_cancelling_job(self):
        with tempfile.TemporaryDirectory(prefix="agent-recovery-") as folder:
            root = Path(folder)
            (root / "metadata").mkdir(); (root / "uploads").mkdir()
            uploading, cancelling = "1" * 32, "2" * 32
            (root / "metadata" / f"{uploading}.json").write_text(json.dumps({"id": uploading, "status": "uploading", "extension": ".csv", "size": 8, "received": 4}), encoding="utf-8")
            (root / "metadata" / f"{cancelling}.json").write_text(json.dumps({"id": cancelling, "status": "cancelling"}), encoding="utf-8")
            (root / "uploads" / f"{uploading}.csv").write_bytes(b"1234tail")
            program = "from app import store; import json; print(json.dumps([store.get('1'*32)['received'], store.get('2'*32)['status']]))"
            result = subprocess.run([sys.executable, "-c", program], cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, DATA_AGENT_DATA_DIR=folder), capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), [4, "interrupted"])
            self.assertEqual((root / "uploads" / f"{uploading}.csv").read_bytes(), b"1234")
