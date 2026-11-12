"""Each agent's own Fish Audio voice: found with
find_voice, chosen by the owner, saved with set_voice, used by voice mode."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import agent_voice
import agents
import chat_service
import llm_client
import vault

VOICE_ID = "e686ae649ee44f219a108aacba206c1a"
ITEM = {"_id": VOICE_ID, "title": "calm storyteller male", "type": "tts", "languages": ["en"],
        "tags": ["male", "deep", "narration"], "description": "A deep and calm voice.",
        "samples": [{"audio": "https://platform.r2.fish.audio/task/x.mp3"}]}


def response(status, body):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


class AgentVoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        registry = patch.dict(agents.AGENTS, {"greg": agents.AgentConfig("greg", "Greg", "Greg", "#6a8f60", "Synthetic test agent")})
        registry.start()
        self.addCleanup(registry.stop)
        for target, name, value in ((agents, "AGENTS_ROOT", root), (vault, "AGENTS_ROOT", root)):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        (root / agents.AGENTS["greg"].folder).mkdir(parents=True)
        self.agent_file = root / agents.AGENTS["greg"].folder / agents.AGENT_FILE
        self.agent_file.write_text(json.dumps({"id": "greg", "name": "Mark", "tagline": "plants"}))
        p = patch.dict("os.environ", {"FISH_AUDIO_API_KEY": "k"})
        p.start()
        self.addCleanup(p.stop)

    def test_find_voice_lists_options_with_samples(self):
        with patch("requests.get", return_value=response(200, {"items": [ITEM, {**ITEM, "type": "svc"}]})) as get:
            out = llm_client._run_tool("greg", "find_voice", {"query": "calm deep male"})
        self.assertEqual(get.call_args.kwargs["params"]["title"], "calm deep male")
        self.assertEqual(get.call_args.kwargs["params"]["language"], "en")
        self.assertIn(f"calm storyteller male (id {VOICE_ID})", out)
        self.assertIn("Sample: https://platform.r2.fish.audio/task/x.mp3", out)
        self.assertEqual(out.count("\n"), 0, "only real TTS voices")

    def test_set_voice_saves_it_and_voice_mode_uses_it(self):
        self.assertIsNone(agent_voice.get("greg"))
        with patch("requests.get", return_value=response(200, ITEM)):
            out = llm_client._run_tool("greg", "set_voice", {"voice_id": VOICE_ID})
        self.assertIn("voice set: calm storyteller male", out)
        data = json.loads(self.agent_file.read_text())
        self.assertEqual(data["voice"], {"provider": "fish_audio", "voice_id": VOICE_ID, "name": "calm storyteller male"})
        self.assertEqual(data["tagline"], "plants", "the rest of agent.json is kept")

        # Voice mode speaks with it (English); Norwegian keeps the Norwegian voice.
        spoke = {}
        def fake_fish(text, voice_id, path):
            spoke["voice"] = voice_id
        with patch.object(chat_service.vault, "get_conversation", return_value=None), \
             patch.object(chat_service.vault, "append_turn"), \
             patch.object(chat_service.vault, "identity_snapshot", return_value=None), \
             patch.object(chat_service.vault, "identity_limit_item_ids", return_value=set()), \
             patch.object(chat_service.vault, "record_response_time"), \
             patch.object(chat_service, "_voice_system_prompt", return_value="sys"), \
             patch.object(chat_service, "_identity_limit_notice", return_value=""), \
             patch.object(chat_service, "_chat_checked", return_value="Hello"), \
             patch.object(chat_service.conversation_titles, "schedule"), \
             patch.object(chat_service.fish_audio_tts, "synthesize", side_effect=fake_fish):
            chat_service._run_voice_reply_locked("greg", "c1", "hi")
            self.assertEqual(spoke["voice"], VOICE_ID)
            import dataclasses
            with patch.object(chat_service, "CONFIG", dataclasses.replace(chat_service.CONFIG, voice_second_language="no", voice_second_language_name="Norwegian (bokmål)", voice_second_voice={"provider": "fish_audio", "voice_id": "ffffffffffffffffffffffffffffffff"})):
                chat_service._run_voice_reply_locked("greg", "c1", "hei", language="no")
            self.assertEqual(spoke["voice"], "f" * 32)

    def test_bad_ids_are_refused(self):
        self.assertIn("refused", llm_client._run_tool("greg", "set_voice", {"voice_id": "../etc"}))
        with patch("requests.get", return_value=response(404, {})):
            self.assertIn("refused", llm_client._run_tool("greg", "set_voice", {"voice_id": "0" * 32}))
        self.assertIsNone(agent_voice.get("greg"))

    def test_without_a_key(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIn("FISH_AUDIO_API_KEY", llm_client._run_tool("greg", "find_voice", {"query": "x"}))

    def test_every_agent_has_the_tools_and_onboarding_asks(self):
        import agent_factory, agent_tools
        self.assertTrue({"find_voice", "set_voice"} <= agent_tools.allowed("greg"))
        directive = agent_factory.onboarding_directive("greg")
        self.assertIn("find_voice", directive)
        self.assertIn("phone page", directive)


if __name__ == "__main__":
    unittest.main()
