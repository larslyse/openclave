import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import chat_service
import service_settings

HISTORY = [{"role": "user", "content": "hi"}]


class FallbackTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(service_settings, "SETTINGS_FILE", Path(tempfile.mkdtemp()) / "s.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _try(self, error, replies):
        with patch("llm_client.chat", side_effect=replies) as chat:
            result = chat_service._try_fallback("reba", "sys", HISTORY, "claude-sonnet-5", error)
        return result, chat

    def test_off_by_default(self):
        result, chat = self._try("credit balance is too low", ["ok"])
        self.assertIsNone(result)
        chat.assert_not_called()

    def test_retries_on_exhausted_and_says_so(self):
        service_settings.update({"fallback_model": "gpt-5.6-terra"})
        result, chat = self._try(RuntimeError("Your credit balance is too low"), ["hello"])
        reply, model_id = result
        self.assertEqual(model_id, "gpt-5.6-terra")
        self.assertTrue(reply.startswith("hello"))
        self.assertIn("Answered by GPT-5.6 Terra", reply)
        self.assertEqual(chat.call_args.kwargs["model_id"], "gpt-5.6-terra")

    def test_no_retry_for_other_errors_same_model_or_failed_fallback(self):
        service_settings.update({"fallback_model": "gpt-5.6-terra"})
        self.assertIsNone(self._try("(error: timeout)", ["x"])[0])
        with patch("llm_client.chat") as chat:
            self.assertIsNone(chat_service._try_fallback("reba", "s", HISTORY, "gpt-5.6-terra", "out of credits"))
        chat.assert_not_called()
        self.assertIsNone(self._try("out of credits", ["(codex-cli error: also broken)"])[0])
        self.assertIsNone(self._try("out of credits", RuntimeError("down"))[0])

    def test_skips_text_only_fallback_when_message_has_an_image(self):
        service_settings.update({"fallback_model": "claude-sonnet-4-6"})
        image_history = [{"role": "user", "content": [{"type": "text", "text": "look"}]}]
        # Every catalog model reads images; the rule still
        # matters for any text-only model added later.
        text_only = chat_service.llm_models.ModelOption(id="claude-sonnet-4-6", provider="claude-cli", display_name="T", tier="t")
        with patch("llm_client.chat") as chat, patch.object(chat_service.llm_models, "get", return_value=text_only):
            self.assertIsNone(chat_service._try_fallback("reba", "s", image_history, "claude-sonnet-5", "out of credits"))
        chat.assert_not_called()

    def test_rejects_unknown_fallback_model(self):
        with self.assertRaises(ValueError):
            service_settings.update({"fallback_model": "gpt-99"})
        service_settings.update({"fallback_model": ""})  # turning it off is always allowed



class FallbackChatPathTests(unittest.TestCase):
    """Through run_chat_locked, in a throwaway agent folder."""

    def setUp(self):
        import agent_factory, agent_tools, agents, llm_models, vault
        tmp = Path(tempfile.mkdtemp())
        (tmp / "Agents").mkdir()
        for patcher in (
            patch.object(service_settings, "SETTINGS_FILE", tmp / "s.json"),
            patch.object(agents, "AGENTS_ROOT", tmp / "Agents"),
            patch.object(vault, "AGENTS_ROOT", tmp / "Agents"),
            patch.object(agent_tools, "SELECTION_FILE", tmp / "t.json"),
            patch.object(llm_models, "SELECTION_FILE", tmp / "m.json"),
            patch.dict(agents.AGENTS),
            patch("retrieval.build_context", return_value=""),
            patch("llm_client.maybe_compact_conversation"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cid = agent_factory.create_agent("Chef", "Meals", "claude-sonnet-5")["conversation_id"]
        self.vault = vault

    def test_exhausted_model_falls_back_and_turn_is_saved(self):
        service_settings.update({"fallback_model": "gpt-5.6-terra"})
        with patch("llm_client.chat", side_effect=[RuntimeError("credit balance is too low"), "Pasta tonight."]):
            reply = chat_service.run_chat_locked("chef", self.cid, "dinner?")
        self.assertIn("Pasta tonight.", reply)
        self.assertIn("Answered by GPT-5.6 Terra", reply)
        turns = self.vault.get_conversation("chef", self.cid)["turns"]
        self.assertIn("Pasta tonight.", turns[-1]["content"])

    def test_without_fallback_the_error_still_surfaces(self):
        with patch("llm_client.chat", return_value="(error: credit balance is too low)"):
            with self.assertRaises(chat_service.ProviderReplyError) as ctx:
                chat_service.run_chat_locked("chef", self.cid, "dinner?")
        self.assertIn("credit", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
