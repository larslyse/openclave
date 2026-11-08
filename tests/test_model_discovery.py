import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import model_discovery


class ModelDiscoveryTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(model_discovery, "CHECK_FILE", Path(tempfile.mkdtemp()) / "check.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reports_only_unknown_chat_models_and_survives_a_failing_source(self):
        def broken():
            raise RuntimeError("down")
        sources = {"A": lambda: ["grok-4.6", "grok-9", "grok-imagine-video"], "B": broken}
        with patch.object(model_discovery, "SOURCES", sources):
            result = model_discovery.check()
        self.assertEqual(result["providers"]["A"]["new"], ["grok-9"])
        self.assertIn("could not check", result["providers"]["B"]["error"])
        self.assertEqual(model_discovery.cached()["providers"]["A"]["new"], ["grok-9"])

    def test_staleness(self):
        self.assertTrue(model_discovery.is_stale(None))
        fresh = {"checked_at": datetime.now(timezone.utc).isoformat()}
        old = {"checked_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()}
        self.assertFalse(model_discovery.is_stale(fresh))
        self.assertTrue(model_discovery.is_stale(old))


if __name__ == "__main__":
    unittest.main()
