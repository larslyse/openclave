import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import retrieval
import service_settings

QMD_OUTPUT = """qmd://notes/agents/reba/tracking.md:26 #36b36e
Title: Reba — Tracking
Score:  93%

| 14-09-2026 | — Sand dunes |


qmd://notes/agents/reba/conversations/20260915-062645.md:11 #ed1089
Title: 20260915-062645
Score:  93%

### User (20:14)
sand dunes


qmd://notes/personal/projects/openclave/build-docs/archive/voice.md:3 #aaaaaa
Title: Voice (old design)
Score:  92%

stale sand dunes note


qmd://notes/agents/reba/backlog.md:15 #4760bd
Title: Purpose
Score:  92%

- [x] Follow up on sand dune session
"""


class AutoLookupTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        patcher = patch.object(service_settings, "SETTINGS_FILE", tmp / "service_settings.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_defaults_to_full_and_rejects_bad_values(self):
        self.assertEqual(service_settings.load()["auto_lookup"], "full")
        with self.assertRaises(ValueError):
            service_settings.update({"auto_lookup": "sometimes"})
        with self.assertRaises(ValueError):
            service_settings.update({"unknown": 1})

    def test_off_never_searches(self):
        service_settings.update({"auto_lookup": "off"})
        with patch("vault.qmd_query") as query:
            self.assertIn("automatic lookup is off", retrieval.build_context("sand dunes"))
        query.assert_not_called()

    def test_full_searches_whole_message_and_drops_conversation_logs(self):
        with patch("vault.qmd_available", return_value=True), \
                patch("vault.qmd_query", return_value=QMD_OUTPUT) as query:
            context = retrieval.build_context("I went to the sand dunes")
        self.assertEqual(query.call_args.args[0], "I went to the sand dunes")
        self.assertIn("tracking.md", context)
        self.assertIn("backlog.md", context)
        self.assertNotIn("/conversations/", context)
        self.assertNotIn("/archive/", context)

    def test_keywords_searches_only_the_rarest_words(self):
        service_settings.update({"auto_lookup": "keywords"})
        with patch.object(retrieval, "message_keywords", return_value=["dunes", "sand"]), \
                patch("vault.qmd_available", return_value=True), \
                patch("vault.qmd_query", return_value=QMD_OUTPUT) as query:
            retrieval.build_context("today I went to the sand dunes")
        self.assertEqual(query.call_args.args[0], "dunes sand")

    def test_keywords_mode_skips_search_when_no_keywords(self):
        service_settings.update({"auto_lookup": "keywords"})
        with patch.object(retrieval, "message_keywords", return_value=[]), patch("vault.qmd_query") as query:
            retrieval.build_context("hi")
        query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
