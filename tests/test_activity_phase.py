"""Progress labels come from tool activity, without extra model output."""
import json
import subprocess
import unittest
from unittest.mock import patch

import llm_client


class ActivityPhaseTests(unittest.TestCase):
    def test_home_assistant_calls_distinguish_reads_from_changes(self):
        get = {"method": "GET", "path": "/api/config"}
        change = {"method": "POST", "path": "/api/services/light/turn_on"}
        for name in ("ha_api", "mcp__openclave-ha-admin__ha_api"):
            self.assertEqual(llm_client.tool_phase(name, get), "reading home assistant")
            self.assertEqual(llm_client.tool_phase(name, change), "updating home assistant")
        self.assertEqual(llm_client.tool_phase("ha_ws", {"message": {"type": "config/entity_registry/get"}}),
                         "reading home assistant")
        self.assertEqual(llm_client.tool_phase("ha_ws", {"message": {"type": "config/entity_registry/remove"}}),
                         "updating home assistant")

    def test_codex_log_and_claude_stream_use_same_labels(self):
        args = json.dumps({"method": "POST", "path": "/api/services/light/turn_on"})
        codex = json.dumps({"type": "response_item", "payload": {"type": "function_call",
                            "name": "mcp__openclave-ha-admin__ha_api", "arguments": args}})
        claude = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__ha-admin__ha_api",
                   "input": json.loads(args)}]}}
        self.assertEqual(llm_client.codex_phase_for(codex), "updating home assistant")
        self.assertEqual(llm_client.claude_phase_for(claude), "updating home assistant")
        self.assertEqual(llm_client.claude_phase_for({"type": "result", "result": "done"}), None)

    def test_claude_stream_keeps_final_reply_and_usage(self):
        def fake_run(args, **kwargs):
            self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
            kwargs["stdout"].write((json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "mcp__ha-admin__ha_api", "input": {"method": "GET"}}]}}) + "\n").encode())
            kwargs["stdout"].write((json.dumps({"type": "result", "result": "done", "usage": {"input_tokens": 1}}) + "\n").encode())
            return subprocess.CompletedProcess(args, 0, None, "")

        with patch.object(llm_client.subprocess, "run", side_effect=fake_run), \
             patch.object(llm_client, "log_usage") as usage:
            self.assertEqual(llm_client._chat_claude_cli("r2", "claude-opus-4-6", "system", []), "done")
        usage.assert_called_once_with("r2", "claude-cli", "claude-opus-4-6", {"input_tokens": 1}, None)


if __name__ == "__main__":
    unittest.main()
