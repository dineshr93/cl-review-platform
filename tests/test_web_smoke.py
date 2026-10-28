"""Browser smoke test over the real UI. Skipped when node or a chromium binary is absent."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
NODE = shutil.which("node")
CHROME = next((c for c in ("chromium", "chromium-browser", "google-chrome") if shutil.which(c)), None)


@unittest.skipIf(not (NODE and CHROME), "web smoke test needs node and a chromium binary")
class WebSmokeTests(unittest.TestCase):
    def test_ui_smoke(self):
        env = dict(os.environ, CL_PYTHON=os.environ.get("CL_PYTHON", sys.executable), CL_CHROME=CHROME)
        # The smoke script waits up to 40s per chromium launch attempt (two attempts) before the
        # page checks start, so the old 240s budget could expire on a slow runner mid-retry.
        proc = subprocess.run([NODE, str(HERE / "web_smoke.mjs")], env=env, cwd=str(HERE.parent),
                              capture_output=True, text=True, timeout=360)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
