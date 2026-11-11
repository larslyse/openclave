"""Release number from VERSION (version.py) in /api/version and run.py --check."""
import contextlib
import io
import re
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import main
import setup_check
import version


class VersionTests(unittest.TestCase):
    def test_version_file_is_semver(self):
        text = (Path(version.__file__).parent / "VERSION").read_text().strip()
        self.assertRegex(text, r"^\d+\.\d+\.\d+$")
        self.assertEqual(version.VERSION, text)

    def test_api_version_reports_it_next_to_the_deploy_fields(self):
        body = TestClient(main.app).get("/api/version").json()
        self.assertEqual(body["version"], version.VERSION)
        for key in ("started_at", "running_commit", "stale", "changed_files", "manager_problem"):
            self.assertIn(key, body)

    def test_check_report_starts_with_the_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup_check.print_report()
        self.assertTrue(re.match(rf"OpenClave version: +{re.escape(version.VERSION)}\n", out.getvalue()))


if __name__ == "__main__":
    unittest.main()
