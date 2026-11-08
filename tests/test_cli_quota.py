import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import llm_client

LINE = json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
    "primary": {"used_percent": 55.0, "window_minutes": 300, "resets_at": 1790199607},
    "secondary": {"used_percent": 25.0, "window_minutes": 10080, "resets_at": 1790715977}}}})


class CliQuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patcher = patch.object(llm_client, "CLI_QUOTA_FILE", self.tmp / "cli_quota.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_records_latest_reading_from_codex_session(self):
        home = self.tmp / "home"
        (home / "sessions" / "2026" / "09" / "24").mkdir(parents=True)
        (home / "sessions" / "2026" / "09" / "24" / "r.jsonl").write_text('{"type":"other"}\n' + LINE + "\n")
        llm_client._record_codex_rate_limits(home)
        saved = json.loads((self.tmp / "cli_quota.json").read_text())["codex"]
        self.assertEqual(saved["primary"]["used_percent"], 55.0)
        self.assertIn("observed_at", saved)

    def test_never_raises_and_writes_nothing_without_data(self):
        llm_client._record_codex_rate_limits(self.tmp / "missing")
        (self.tmp / "bad" / "sessions").mkdir(parents=True)
        (self.tmp / "bad" / "sessions" / "x.jsonl").write_text('{"rate_limits": broken\n')
        llm_client._record_codex_rate_limits(self.tmp / "bad")
        self.assertFalse((self.tmp / "cli_quota.json").exists())


if __name__ == "__main__":
    unittest.main()


class ApiEquivalentTests(unittest.TestCase):
    def test_subscription_usage_priced_at_api_list_price(self):
        import llm_client, llm_models
        m = next(x for x in llm_models.CATALOG if x.api_input_usd is not None and x.api_output_usd is not None)
        entry = {"model_id": m.id, "input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000, "output_tokens": 1_000_000}
        self.assertAlmostEqual(llm_client.api_equivalent_usd(entry), m.api_input_usd * 1.1 + m.api_output_usd)
        self.assertIsNone(llm_client.api_equivalent_usd({"model_id": "no-such-model"}))
