"""Conversation moves must preserve history; model timings stay bounded."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import chat_jobs
import chat_service
import vault


class MoveAndTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "Agents"
        root.mkdir()
        patcher = patch.object(vault, "AGENTS_ROOT", root)
        patcher.start()
        self.addCleanup(patcher.stop)
        for agent in ("reba", "rick"):
            vault.agent_root(agent).mkdir()

    def test_move_preserves_old_speaker_attachment_and_archive(self):
        cid = "20260923-120000-abcdef12"
        vault.append_turn("reba", cid, "user", "Look at this", first_message="Look at this")
        vault.append_turn("reba", cid, "assistant", "I see it.")
        attachment = vault.save_attachment("reba", cid, "photo.png", "image/png", b"image")
        vault.append_turn("reba", cid, "user", vault.attachment_markdown_ref(cid, attachment))
        archive = vault.conversations_dir("reba") / "archive" / f"{cid}-pre-compaction-123.md"
        archive.parent.mkdir(parents=True)
        archive.write_text("archived original")

        self.assertTrue(chat_jobs.move_conversation("reba", "rick", cid))
        self.assertIsNone(vault.get_conversation("reba", cid))
        moved = vault.get_conversation("rick", cid)
        self.assertEqual([t["role"] for t in moved["turns"]], ["user", "assistant", "user"])
        self.assertEqual(moved["turns"][1]["content"], "I see it.")
        self.assertEqual(moved["turns"][2]["attachments"][0]["filename"], "photo.png")
        self.assertEqual(vault.read_attachment("rick", cid, attachment["id"])[0], b"image")
        self.assertTrue((vault.conversations_dir("rick") / "archive" / archive.name).exists())
        vault.append_turn("rick", cid, "assistant", "Now I own this thread.")
        self.assertEqual(vault.get_conversation("rick", cid)["turns"][-1]["content"], "Now I own this thread.")

    def test_target_collision_leaves_source_untouched(self):
        cid = "20260923-120000-abcdef12"
        vault.append_turn("reba", cid, "user", "Source")
        vault.append_turn("rick", cid, "user", "Target")
        with self.assertRaises(FileExistsError):
            chat_jobs.move_conversation("reba", "rick", cid)
        self.assertIsNotNone(vault.get_conversation("reba", cid))
        self.assertEqual(vault.get_conversation("rick", cid)["turns"][0]["content"], "Target")

    def test_response_time_average_uses_last_five(self):
        for duration in (100, 200, 300, 400, 500, 600):
            vault.record_response_time("reba", "model-a", duration)
        self.assertEqual(vault.read_response_time_averages("reba")["model-a"],
                         {"average_ms": 400, "samples": 5})

    def test_credit_error_is_distinct_from_temporary_rate_limit(self):
        with patch.object(chat_service.llm_models, "selected_model_id", return_value="grok-4.3"):
            self.assertIn("provider credit or usage limit is exhausted",
                          chat_service._provider_failure("reba", "insufficient_quota"))
            self.assertEqual(chat_service._provider_failure("reba", "429 rate limit reached"),
                             "429 rate limit reached")




# Finished chat jobs send a reply push; the real subscriptions are the owner's phones.
_no_push = patch("web_push.send_all", return_value=0)


def setUpModule():
    _no_push.start()


def tearDownModule():
    _no_push.stop()

if __name__ == "__main__":
    unittest.main()
