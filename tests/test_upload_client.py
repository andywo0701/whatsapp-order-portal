"""Client upload script: live progress, per-piece timeout, and resume."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class UploadClientScriptTests(unittest.TestCase):
    def test_progress_timeout_and_resume(self) -> None:
        script = ROOT / "tests" / "upload_client.test.js"
        result = subprocess.run(
            ["node", "--test", str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)


if __name__ == "__main__":
    unittest.main()
