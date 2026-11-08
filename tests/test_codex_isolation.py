import base64
import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import llm_client


class CodexIsolationTests(unittest.TestCase):
    def test_r2_gets_ha_admin_without_exposing_it_to_other_agents(self):
        configs = {}

        def fake_run(args, **kwargs):
            configs[args[args.index("-C") + 1]] = tomllib.loads(
                (Path(kwargs["env"]["CODEX_HOME"]) / "config.toml").read_text()
            )
            self.assertEqual("--approve-for-me" in args, args[args.index("-C") + 1].endswith("/r2"))
            self.assertEqual("-s" not in args, args[args.index("-C") + 1].endswith("/r2"))
            Path(args[args.index("--output-last-message") + 1]).write_text("ok")
            return subprocess.CompletedProcess(args, 0, "", "")

        with tempfile.TemporaryDirectory() as source_home, tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"CODEX_HOME": source_home, "HA_API_TOKEN": "test-token"}), \
                 patch.object(llm_client.vault, "agent_root", side_effect=lambda agent: Path(root) / agent), \
                 patch.object(llm_client.subprocess, "run", side_effect=fake_run):
                for agent in ("r2", "reba"):
                    llm_client._chat_codex_cli(agent, "gpt-5.6-terra", "system", [])
            r2 = configs[str(Path(root) / "r2")]["mcp_servers"]
            reba = configs[str(Path(root) / "reba")]["mcp_servers"]
            self.assertIn("openclave-ha-admin", r2)
            self.assertNotIn("openclave-ha-admin", reba)
            self.assertEqual(r2["openclave-ha-admin"]["env_vars"], ["HA_API_TOKEN"])
            self.assertEqual(r2["openclave-ha-admin"]["default_tools_approval_mode"], "auto")
            self.assertTrue(r2["openclave-ha-admin"]["required"])
            self.assertNotIn("test-token", str(r2))

    def test_codex_call_uses_only_the_selected_agents_mcp_server(self):
        with tempfile.TemporaryDirectory() as source_home, tempfile.TemporaryDirectory() as agent_root:
            source_auth = Path(source_home) / "auth.json"
            source_auth.write_text('{"test": true}')

            def fake_run(args, **kwargs):
                isolated_home = Path(kwargs["env"]["CODEX_HOME"])
                config = (isolated_home / "config.toml").read_text()
                self.assertIn("openclave-vault-reba", config)
                self.assertNotIn("openclave-vault-spice", config)
                self.assertEqual((isolated_home / "auth.json").resolve(), source_auth)
                self.assertNotIn("XDG_CONFIG_HOME", kwargs["env"])
                self.assertNotIn("XDG_CACHE_HOME", kwargs["env"])

                output_path = Path(args[args.index("--output-last-message") + 1])
                output_path.write_text("isolated reply")
                return subprocess.CompletedProcess(args, 0, "", "")

            with (
                patch.dict(
                    os.environ,
                    {
                        "CODEX_HOME": source_home,
                        "XDG_CONFIG_HOME": "/unrelated/config",
                        "XDG_CACHE_HOME": "/unrelated/cache",
                    },
                    clear=False,
                ),
                patch.object(llm_client.vault, "agent_root", return_value=Path(agent_root)),
                patch.object(llm_client.subprocess, "run", side_effect=fake_run),
            ):
                reply = llm_client._chat_codex_cli("reba", "gpt-5.6-terra", "system", [])

        self.assertEqual(reply, "isolated reply")

    def test_images_are_passed_as_files_and_cleaned_up(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                paths = []
                images = [b"first image bytes", b"second image bytes"]
                messages = [{"role": "user", "content": [
                    {"type": "text", "text": "Read these labels"},
                    *[{"type": "image", "media_type": "image/png",
                       "data": base64.b64encode(data).decode()} for data in images],
                ]}]

                def fake_run(args, **kwargs):
                    image_paths = [Path(args[i + 1]) for i, arg in enumerate(args) if arg == "--image"]
                    paths.extend(image_paths)
                    paths.append(Path(args[args.index("--output-last-message") + 1]))
                    self.assertEqual([p.read_bytes() for p in image_paths], images)
                    self.assertEqual(args[-2], "--")
                    self.assertIn("Read these labels", args[-1])
                    self.assertIn("[Attached image 2]", args[-1])
                    for data in images:
                        self.assertNotIn(base64.b64encode(data).decode(), args[-1])
                    if timeout:
                        raise subprocess.TimeoutExpired(args, 1)
                    paths[-1].write_text("labels read")
                    return subprocess.CompletedProcess(args, 0, "", "")

                with patch.object(llm_client.subprocess, "run", side_effect=fake_run):
                    if timeout:
                        with patch.object(llm_client, "cancel_running"):
                            reply = llm_client._chat_codex_cli("stash", "gpt-5.6-sol", "system", messages)
                        self.assertIn("no reply from GPT-5.6 Sol within 5 minutes", reply)
                    else:
                        self.assertEqual(llm_client._chat_codex_cli("stash", "gpt-5.6-sol", "system", messages), "labels read")
                self.assertTrue(paths)
                self.assertTrue(all(not p.exists() for p in paths))
                self.assertEqual(messages[0]["content"][1]["type"], "image")

    def test_codex_models_enable_attachment_interface(self):
        for model in llm_client.llm_models.CATALOG:
            if model.provider == "codex-cli":
                self.assertTrue(model.supports_vision, model.id)


if __name__ == "__main__":
    unittest.main()


class WebSearchSwitchTests(unittest.TestCase):
    def test_codex_web_search_follows_the_agents_tool_selection(self):
        import agent_tools
        import chat_service
        for on in (True, False):
            with self.subTest(on=on), tempfile.TemporaryDirectory() as tmp, \
                 tempfile.TemporaryDirectory() as source_home, tempfile.TemporaryDirectory() as agent_root:
                seen = {}

                def fake_run(args, **kwargs):
                    seen["config"] = (Path(kwargs["env"]["CODEX_HOME"]) / "config.toml").read_text()
                    Path(args[args.index("--output-last-message") + 1]).write_text("ok")
                    return subprocess.CompletedProcess(args, 0, "", "")

                with patch.object(agent_tools, "SELECTION_FILE", Path(tmp) / "agent_tools.json"), \
                     patch.dict(os.environ, {"CODEX_HOME": source_home}, clear=False), \
                     patch.object(llm_client.vault, "agent_root", return_value=Path(agent_root)), \
                     patch.object(llm_client.subprocess, "run", side_effect=fake_run), \
                     patch.object(chat_service.llm_models, "selected_model_id", return_value="gpt-5.6-terra"):
                    agent_tools.set_optional("r2", ["web_search"] if on else ["write_note"])
                    llm_client._chat_codex_cli("r2", "gpt-5.6-terra", "system", [])
                    prompt_line = chat_service._web_line("r2")
                self.assertIn('web_search = "live"' if on else 'web_search = "disabled"', seen["config"])
                self.assertEqual(bool(prompt_line), on)


class CodexLogTests(unittest.TestCase):
    """Lines shaped like a real Codex 0.156 session log (2026-09-25)."""
    TOKEN = json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": 13489, "cached_input_tokens": 11008, "output_tokens": 78, "reasoning_output_tokens": 0, "total_tokens": 13567}}}})
    WEB = json.dumps({"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": "const r = await tools.web__run({op: 'search'})"}})
    NOTE = json.dumps({"type": "response_item", "payload": {"type": "function_call", "name": "mcp__openclave-vault-reba__search_vault", "arguments": "{}"}})
    PATCH = json.dumps({"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin Patch"}})
    THINK = json.dumps({"type": "response_item", "payload": {"type": "reasoning", "summary": []}})

    def test_usage_is_the_last_cumulative_total(self):
        early = self.TOKEN.replace("13489", "100").replace("11008", "0")
        usage = llm_client.codex_usage_from_log("\n".join([early, self.WEB, self.TOKEN]))
        self.assertEqual(usage, {"input_tokens": 13489 - 11008, "cache_read_input_tokens": 11008, "output_tokens": 78})
        self.assertIsNone(llm_client.codex_usage_from_log(self.WEB))

    def test_phase_from_log_lines(self):
        self.assertEqual(llm_client.codex_phase_for(self.WEB), "searching the web")
        self.assertEqual(llm_client.codex_phase_for(self.NOTE), "reading notes")
        self.assertEqual(llm_client.codex_phase_for(self.PATCH), "writing files")
        self.assertEqual(llm_client.codex_phase_for(self.THINK), "thinking")
        self.assertIsNone(llm_client.codex_phase_for(self.TOKEN))

    def test_codex_call_logs_usage_from_its_session(self):
        with tempfile.TemporaryDirectory() as source_home, tempfile.TemporaryDirectory() as agent_root:
            def fake_run(args, **kwargs):
                sessions = Path(kwargs["env"]["CODEX_HOME"]) / "sessions" / "2026" / "09" / "27"
                sessions.mkdir(parents=True)
                (sessions / "rollout.jsonl").write_text(self.WEB + "\n" + self.TOKEN + "\n")
                Path(args[args.index("--output-last-message") + 1]).write_text("ok")
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch.dict(os.environ, {"CODEX_HOME": source_home}, clear=False), \
                 patch.object(llm_client.vault, "agent_root", return_value=Path(agent_root)), \
                 patch.object(llm_client.subprocess, "run", side_effect=fake_run), \
                 patch.object(llm_client, "CLI_QUOTA_FILE", Path(source_home) / "cli_quota.json"), \
                 patch.object(llm_client, "log_usage") as logged:
                llm_client._chat_codex_cli("reba", "gpt-5.6-terra", "system", [])
        logged.assert_called_once_with("reba", "codex-cli", "gpt-5.6-terra",
                                       {"input_tokens": 2481, "cache_read_input_tokens": 11008, "output_tokens": 78}, None)
