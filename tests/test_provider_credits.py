import unittest
from unittest.mock import patch

import provider_credits


class ProviderCreditsTests(unittest.TestCase):
    def setUp(self):
        provider_credits._cache.update(at=0.0, data=None)
        self.addCleanup(provider_credits._cache.update, at=0.0, data=None)

    def test_failing_check_reports_unknown_and_others_still_run(self):
        def boom():
            raise RuntimeError("down")
        checks = {"A": boom, "B": lambda: {"status": "ok", "detail": "fine"}}
        with patch.object(provider_credits, "CHECKS", checks):
            data = provider_credits.get(force=True)
        self.assertEqual(data["A"]["status"], "unknown")
        self.assertEqual(data["B"]["status"], "ok")
        self.assertEqual(data["Anthropic API"]["status"], "not exposed")

    def test_results_are_cached(self):
        calls = []
        checks = {"A": lambda: calls.append(1) or {"status": "ok", "detail": ""}}
        with patch.object(provider_credits, "CHECKS", checks):
            provider_credits.get(force=True)
            provider_credits.get()
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
