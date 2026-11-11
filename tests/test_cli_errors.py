import json
import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import chat_service
import llm_client
from providers import xai_api


PROMPT_ECHO = "OpenAI Codex v1\nuser\n<system_instructions>\nsecret-ish notes " + "x" * 2000 + "\n### Assistant\n"


class CliDiagnosticTests(unittest.TestCase):
    def test_codex_error_line_survives_a_long_prompt_echo(self):
        detail = llm_client._cli_diagnostic(PROMPT_ECHO + "ERROR: Selected model is at capacity.\n", 1)
        self.assertEqual(detail, "exit code 1: ERROR: Selected model is at capacity.")

    def test_without_error_lines_only_a_short_tail_is_kept(self):
        detail = llm_client._cli_diagnostic(PROMPT_ECHO + "y" * 900 + "\nboom\n", 2)
        self.assertLessEqual(len(detail), 520)
        self.assertTrue(detail.endswith("boom"))
        self.assertNotIn("secret-ish", detail)

    def test_claude_cli_failure_reads_the_reason_from_its_output(self):
        stream = "\n".join(json.dumps(e) for e in [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "error": "model_not_found", "message": {"content": [{"type": "text", "text": "There's an issue with the selected model."}]}},
            {"type": "result", "subtype": "success", "is_error": True, "result": "There's an issue with the selected model (x). Run --model to pick a different model."},
        ])

        def fake_run(args, **kwargs):
            kwargs["stdout"].write(stream.encode())
            return subprocess.CompletedProcess(args, 1, None, "")
        with patch.object(llm_client.subprocess, "run", side_effect=fake_run), \
             patch.object(llm_client, "_watch_claude_phase", lambda *a: None):
            reply = llm_client._chat_claude_cli("rick", "claude-x", "system", [{"role": "user", "content": "hi"}])
        self.assertEqual(reply, "(claude-cli error: exit code 1: There's an issue with the selected model (x). Run --model to pick a different model.)")
        self.assertEqual(llm_client._claude_stream_error(stream.split("\n", 2)[0] + "\n" + stream.split("\n")[1]),
                         "model_not_found: There's an issue with the selected model.")
        self.assertEqual(llm_client._claude_stream_error(""), "")

    def test_claude_cli_sends_images_through_stream_json(self):
        seen = {}
        def fake_run(args, **kwargs):
            seen["args"], seen["input"] = args, kwargs.get("input")
            kwargs["stdout"].write(json.dumps({"type": "result", "is_error": False, "result": "Red"}).encode())
            return subprocess.CompletedProcess(args, 0, None, "")
        messages = [{"role": "user", "content": [{"type": "text", "text": "colour?"},
                                                  {"type": "image", "media_type": "image/png", "data": "QUJD"}]}]
        with patch.object(llm_client.subprocess, "run", side_effect=fake_run), \
             patch.object(llm_client, "_watch_claude_phase", lambda *a: None), patch.object(llm_client, "log_usage"):
            self.assertEqual(llm_client._chat_claude_cli("r2", "claude-opus-5-5", "system", messages), "Red")
        self.assertIn("--input-format", seen["args"])
        content = json.loads(seen["input"])["message"]["content"]
        self.assertIn("[Attached image 1]", content[0]["text"])
        self.assertEqual(content[1]["source"], {"type": "base64", "media_type": "image/png", "data": "QUJD"})

    def test_claude_cli_text_only_keeps_the_plain_prompt(self):
        seen = {}
        def fake_run(args, **kwargs):
            seen["args"], seen["kwargs"] = args, kwargs
            kwargs["stdout"].write(json.dumps({"type": "result", "is_error": False, "result": "ok"}).encode())
            return subprocess.CompletedProcess(args, 0, None, "")
        with patch.object(llm_client.subprocess, "run", side_effect=fake_run), \
             patch.object(llm_client, "_watch_claude_phase", lambda *a: None), patch.object(llm_client, "log_usage"):
            llm_client._chat_claude_cli("r2", "claude-opus-5-5", "system", [{"role": "user", "content": "hi"}])
        self.assertNotIn("--input-format", seen["args"])
        self.assertIs(seen["kwargs"]["stdin"], subprocess.DEVNULL)

    def test_xai_gets_images_as_image_url_parts(self):
        sent = {}
        chunk = SimpleNamespace(usage=None, choices=[SimpleNamespace(
            delta=SimpleNamespace(content="Red", tool_calls=None), finish_reason="stop")])
        class Completions:
            def create(self, **kw):
                sent.update(kw); return [chunk]
        fake = type("C", (), {"chat": type("Ch", (), {"completions": Completions()})()})()
        messages = [{"role": "user", "content": [{"type": "text", "text": "colour?"},
                                                  {"type": "image", "media_type": "image/png", "data": "QUJD"}]}]
        with patch.dict(os.environ, {"XAI_API_KEY": "k"}), patch("openai.OpenAI", return_value=fake), \
             patch.object(xai_api, "log_usage"), patch.object(xai_api, "tools_for", return_value=[]):
            self.assertEqual(llm_client._chat_xai("rick", "grok-4.3", "system", messages), "Red")
        parts = sent["messages"][-1]["content"]
        self.assertEqual(parts[1], {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}})

    def test_slow_models_get_more_time(self):
        self.assertEqual(llm_client._cli_timeout("gpt-6-astra", 300), llm_client.SLOW_MODEL_CLI_TIMEOUT_S)
        self.assertEqual(llm_client._cli_timeout("gpt-6-sol", 300), 300)

    def test_codex_timeout_is_a_short_reply_not_the_command_line(self):
        def fake_run(args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])

        with tempfile.TemporaryDirectory() as source_home, tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"CODEX_HOME": source_home}), \
                 patch.object(llm_client.vault, "agent_root", side_effect=lambda agent: Path(root) / agent), \
                 patch.object(llm_client.subprocess, "run", side_effect=fake_run), \
                 patch.object(llm_client, "cancel_running") as cancel:
                reply = llm_client._chat_codex_cli("cathie", "gpt-6-astra", "system", [{"role": "user", "content": "PROMPT-TEXT"}])
        cancel.assert_called_once_with("cathie")
        self.assertTrue(reply.startswith("(codex-cli error: no reply from GPT-6 Astra within 10 minutes"))
        self.assertNotIn("PROMPT-TEXT", reply)
        self.assertNotIn("codex", reply.split("error:", 1)[1].lower())

    def test_codex_failure_shows_the_error_not_the_prompt(self):
        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, "", PROMPT_ECHO + "ERROR: Selected model is not available\n")

        with tempfile.TemporaryDirectory() as source_home, tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"CODEX_HOME": source_home}), \
                 patch.object(llm_client.vault, "agent_root", side_effect=lambda agent: Path(root) / agent), \
                 patch.object(llm_client.subprocess, "run", side_effect=fake_run):
                reply = llm_client._chat_codex_cli("r2", "gpt-5.6-sol", "system", [])
        self.assertEqual(reply, "(codex-cli error: exit code 1: ERROR: Selected model is not available)")

    def test_unavailable_model_gets_an_actionable_message(self):
        with patch.object(chat_service.llm_models, "selected_model_id", return_value="gpt-5.6-sol"):
            text = chat_service._provider_failure("r2", "(codex-cli error: exit code 1: ERROR: Selected model is not available)")
        self.assertIn("GPT-5.6 Sol isn't available to this account right now", text)
        self.assertIn("ERROR: Selected model", text)

    def test_claude_cli_route_uses_the_catalog_speed_not_the_api_id(self):
        # claude-opus-5-5-cli runs as "claude-opus-5-5" (a fast API entry); the
        # 10-minute limit must still apply (R2's HA build stopped at 5, 2026-10-17).
        with patch.object(llm_client, "_chat_claude_cli", return_value="ok") as run:
            llm_client.chat("r2", "system", [], model_id="claude-opus-5-5-cli")
        self.assertEqual(run.call_args.args[1], "claude-opus-5-5")
        self.assertEqual(run.call_args.kwargs["timeout_s"], llm_client.SLOW_MODEL_CLI_TIMEOUT_S)

    def test_claude_timeout_lists_the_steps_already_done(self):
        stream = [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a", "name": "mcp__ha__ha_ws", "input": {"type": "input_boolean/create", "name": "Dim"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "content": "ok"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "b", "name": "mcp__ha__ha_api", "input": {"path": "x"}}]}},
        ]

        def fake_run(args, **kwargs):
            kwargs["stdout"].write("\n".join(json.dumps(e) for e in stream).encode() + b"\n")
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])

        with patch.object(llm_client.subprocess, "run", side_effect=fake_run), \
             patch.object(llm_client, "cancel_running"), patch.object(llm_client, "_extra_servers", return_value={}), \
             patch.object(llm_client, "_has_web_search", return_value=False):
            reply = llm_client._chat_claude_cli("r2", "claude-opus-5-5", "system", [{"role": "user", "content": "build"}], timeout_s=600)
        self.assertTrue(reply.startswith("**Stopped after 10 minutes before Claude Opus 5.5 finished.**"))
        self.assertIn('ha_ws {"type": "input_boolean/create", "name": "Dim"}', reply)
        self.assertNotIn("ha_api", reply)  # started, never finished

    def test_codex_steps_from_the_session_log(self):
        with tempfile.TemporaryDirectory() as home:
            log = Path(home) / "sessions" / "2026" / "s.jsonl"
            log.parent.mkdir(parents=True)
            log.write_text("\n".join(json.dumps({"payload": p}) for p in [
                {"type": "function_call", "call_id": "1", "name": "ha_ws", "arguments": '{"type": "automation/config"}'},
                {"type": "function_call_output", "call_id": "1", "output": "ok"},
                {"type": "custom_tool_call", "call_id": "2", "name": "exec", "input": "never finished"},
            ]))
            self.assertEqual(llm_client.codex_steps_done(Path(home)), ['ha_ws {"type": "automation/config"}'])


if __name__ == "__main__":
    unittest.main()
