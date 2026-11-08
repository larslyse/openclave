"""Runs the mobile app's Node checks (tests/*.cjs) as part of the normal
`python3 -m unittest discover -s tests` run, so they can't be skipped."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("node"), "node not installed")
class MobileJsTests(unittest.TestCase):
    def test_node_checks(self):
        for script in sorted((ROOT / "tests").glob("*.cjs")):
            with self.subTest(script=script.name):
                result = subprocess.run(["node", str(script)], cwd=ROOT, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
