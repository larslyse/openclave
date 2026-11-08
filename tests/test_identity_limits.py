"""Identity limits must preserve the proposed change and tell the owner when blocked."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import chat_service
import llm_client
import vault


class IdentityLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Agents"
        self.root.mkdir()
        self.patch_root = patch.object(vault, "AGENTS_ROOT", self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        vault.agent_root("reba").mkdir()
        vault.agent_root("rick").mkdir()

    def test_feedback_over_limit_is_queued_and_not_written(self):
        path = vault.persona_file("reba")
        original = "# Persona\n" + "x" * 2140 + "\n"
        path.write_text(original)

        result = llm_client._run_tool("reba", "add_feedback", {"entry": "Please be clearer."})

        self.assertIn("not stored: character limit reached", result)
        self.assertEqual(path.read_text(), original)
        items = vault.read_maintainer_inbox()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["reason"], "identity_char_limit")
        self.assertIn("Please be clearer.", items[0]["summary"])

    def test_direct_over_limit_edit_is_restored_and_notice_is_generated(self):
        path = vault.mission_file("reba")
        original = "# Mission\nOriginal.\n"
        path.write_text(original)
        before = vault.identity_snapshot("reba")
        previous_ids = vault.identity_limit_item_ids("reba")
        path.write_text("# Mission\n" + "x" * 1900)

        notice = chat_service._identity_limit_notice("reba", previous_ids, before)

        self.assertEqual(path.read_text(), original)
        self.assertIn("Mission not stored: character limit reached. Sent to the maintainer (id:", notice)
        self.assertIn("x" * 100, vault.read_maintainer_inbox()[0]["summary"])

    def test_chat_reply_contains_notice_even_when_agent_omits_it(self):
        path = vault.persona_file("reba")
        path.write_text("# Persona\n" + "x" * 2140 + "\n")

        def agent_reply(*args, **kwargs):
            llm_client._run_tool("reba", "add_feedback", {"entry": "Please be clearer."})
            return "Understood."

        with patch.object(chat_service.retrieval, "build_context", return_value=""), \
             patch.object(chat_service, "_system_prompt", return_value=""), \
             patch.object(chat_service.llm_models, "selected_model_id", return_value="grok-4.3"), \
             patch.object(chat_service.llm_client, "chat", side_effect=agent_reply):
            reply = chat_service.run_chat("reba", "20260923-120000-abcdef12", "Be clearer")

        self.assertIn("Understood.", reply)
        self.assertIn("not stored: character limit reached. Sent to the maintainer (id:", reply)
        self.assertEqual(vault.get_conversation("reba", "20260923-120000-abcdef12")["turns"][-1]["content"], reply)

    def test_valid_mission_update_is_saved_without_inbox_item(self):
        vault.update_mission("reba", "# Mission\nUpdated.")
        self.assertIn("Updated.", vault.mission_file("reba").read_text())
        self.assertEqual(vault.read_maintainer_inbox(), [])


if __name__ == "__main__":
    unittest.main()
