"""Safety net for splitting llm_client.py: its public surface, and the exact
subprocess calls the two CLI routes make, recorded from the code before the
split (fixtures/llm_client_surface.json, fixtures/llm_cli_calls.json)."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import llm_client
import llm_models
import providers
import vault
from providers import anthropic_api, claude_cli, codex_cli, xai_api

FIXTURES = Path(__file__).parent / "fixtures"
SURFACE_FILE = FIXTURES / "llm_client_surface.json"
CALLS_FILE = FIXTURES / "llm_cli_calls.json"
REPO = str(Path(__file__).resolve().parent.parent)
TMP = tempfile.gettempdir()
IMAGE = {"type": "image", "media_type": "image/png", "data": "QUJD"}
CASES = {
    "rick-text": ("rick", [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                           {"role": "user", "content": "Title: x\nmore"}], {}),
    "r2-text": ("r2", [{"role": "user", "content": "lights?"}], {}),
    "r2-image": ("r2", [{"role": "user", "content": [{"type": "text", "text": "what is this?"}, IMAGE]}], {}),
    "r2-read-only-run-env": ("r2", [{"role": "user", "content": "check"}], {"read_only": True, "run_env": {"X_APPROVED": "1"}}),
}
ENV = {"ANTHROPIC_API_KEY": "a-key", "OPENAI_API_KEY": "o-key", "XDG_CONFIG_HOME": "/x/config",
       "XDG_CACHE_HOME": "/x/cache", "HA_API_TOKEN": "ha-token"}


def surface() -> list[str]:
    return sorted(n for n in dir(llm_client) if not n.startswith("__"))


def _norm(value):
    if isinstance(value, dict):
        return {str(k): _norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    if not isinstance(value, str):
        return value if isinstance(value, (int, float, bool)) or value is None else type(value).__name__
    value = re.sub(re.escape(TMP) + r"/(openclave-codex-\w+?-)?\w+(\.\w+)?", r"<tmp>/\1*\2", value)
    value = value.replace(sys.executable, "<python>").replace(REPO, "<repo>")
    if value in (llm_client.CLAUDE_BIN, llm_client.CODEX_BIN):
        return "<claude>" if value == llm_client.CLAUDE_BIN else "<codex>"
    value = re.sub(r"OPENCLAVE_RUN=\S+|^(\w+):[0-9a-f]{32}$", lambda m: "<run-marker>", value)
    return "<tmp>" if value == TMP else value


def _record(args, kwargs, extra) -> dict:
    env = kwargs["env"]
    return _norm({
        "argv": list(args),
        "kwargs": {k: v for k, v in sorted(kwargs.items()) if k != "env"},
        "env_removed": sorted(k for k in os.environ if k not in env),
        "env_added": {k: env[k] for k in sorted(env) if k not in os.environ},
        **extra,
    })


def _run_case(route: str, agent_id: str, messages: list, opts: dict) -> dict:
    seen = {}

    def fake_run(args, **kwargs):
        extra = {}
        if route == "claude":
            extra["mcp_config"] = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())
        else:
            home = Path(kwargs["env"]["CODEX_HOME"])
            extra["config_toml"] = (home / "config.toml").read_text()
            extra["codex_home_files"] = sorted(p.name for p in home.iterdir())
            arg = next(a for a in args if a.startswith("model_instructions_file="))
            extra["instructions"] = Path(json.loads(arg.split("=", 1)[1])).read_text()
            Path(args[args.index("--output-last-message") + 1]).write_text("ok")
        seen["call"] = _record(args, kwargs, extra)
        return subprocess.CompletedProcess(args, 0, "", "")

    with tempfile.TemporaryDirectory() as codex_source, \
            patch.dict(os.environ, {**ENV, "CODEX_HOME": codex_source}), \
            patch.object(vault, "agent_root", lambda agent: Path("/agents") / agent), \
            patch.object(subprocess, "run", side_effect=fake_run):
        if opts.get("read_only"):
            llm_client.READ_ONLY_RUNS.add(agent_id)
        if opts.get("run_env"):
            llm_client.RUN_ENV[agent_id] = opts["run_env"]
        try:
            if route == "claude":
                reply = llm_client._chat_claude_cli(agent_id, "claude-opus-5-5", "System prompt.", messages)
            else:
                reply = llm_client._chat_codex_cli(agent_id, "gpt-5.6-terra", "System prompt.", messages)
        finally:
            llm_client.READ_ONLY_RUNS.discard(agent_id)
            llm_client.RUN_ENV.pop(agent_id, None)
    return {**seen["call"], "reply": reply}


def capture() -> dict:
    return {f"{route}/{name}": _run_case(route, *case) for route in ("claude", "codex") for name, case in CASES.items()}


class FacadeSurfaceTests(unittest.TestCase):
    def test_every_name_from_before_the_split_is_still_there(self):
        before = json.loads(SURFACE_FILE.read_text())
        missing = [name for name in before if not hasattr(llm_client, name)]
        self.assertEqual(missing, [])


class CliCallTests(unittest.TestCase):
    maxDiff = None

    def test_claude_and_codex_subprocess_calls_are_unchanged(self):
        expected = json.loads(CALLS_FILE.read_text())
        actual = capture()
        self.assertEqual(sorted(actual), sorted(expected))
        for key in expected:
            with self.subTest(case=key):
                self.assertEqual(actual[key], expected[key])


class DispatchTests(unittest.TestCase):
    """chat() picks the provider by the model's provider id and calls each
    route's function with exactly the arguments the old if/elif chain used."""
    MESSAGES = [{"role": "user", "content": "hi"}]

    def model(self, provider):
        return next(m for m in llm_models.CATALOG if m.provider == provider)

    def test_each_provider_id_reaches_its_class(self):
        expected = {"anthropic": anthropic_api.AnthropicApiProvider, "xai": xai_api.XaiApiProvider,
                    "claude-cli": claude_cli.ClaudeCliProvider, "codex-cli": codex_cli.CodexCliProvider}
        self.assertEqual(set(providers.PROVIDERS), set(expected))
        for provider_id, cls in expected.items():
            with self.subTest(provider=provider_id):
                provider = providers.PROVIDERS[provider_id]
                self.assertIs(type(provider), cls)
                model = self.model(provider_id)
                self.assertEqual(provider.route, model.route)
                with patch.object(cls, "chat", return_value="reply") as chat:
                    self.assertEqual(llm_client.chat("rick", "sys", self.MESSAGES, model_id=model.id,
                                                     max_tokens=7, effort="low"), "reply")
                chat.assert_called_once_with("rick", model, "sys", self.MESSAGES, max_tokens=7, effort="low")

    def test_unknown_provider_ids_go_to_xai_as_before(self):
        odd = llm_models.ModelOption(id="odd-1", provider="something-else", display_name="Odd", tier="t")
        with patch.object(llm_models, "get", return_value=odd), \
                patch.object(xai_api, "_chat_xai", return_value="x") as run:
            self.assertEqual(llm_client.chat("rick", "sys", self.MESSAGES, model_id="odd-1"), "x")
        run.assert_called_once_with("rick", "odd-1", "sys", self.MESSAGES, api_model_id=None, reasoning_effort=None)

    def test_route_functions_get_the_same_arguments_as_before(self):
        grok = llm_models.get("grok-4.6-fast")
        cases = [
            (xai_api, "_chat_xai", grok.id, ("rick", grok.id, "sys", self.MESSAGES),
             {"api_model_id": grok.api_model_id, "reasoning_effort": grok.reasoning_effort}),
            (anthropic_api, "_chat_anthropic", "claude-sonnet-5", ("rick", "claude-sonnet-5", "sys", self.MESSAGES),
             {"max_tokens": 7, "effort": "low"}),
            (claude_cli, "_chat_claude_cli", "claude-opus-5-5-cli", ("rick", "claude-opus-5-5", "sys", self.MESSAGES),
             {"timeout_s": llm_client.SLOW_MODEL_CLI_TIMEOUT_S}),
            (claude_cli, "_chat_claude_cli", "claude-sonnet-4-6", ("rick", "claude-sonnet-4-6", "sys", self.MESSAGES),
             {"timeout_s": llm_client._cli_timeout("claude-sonnet-4-6", llm_client.CLAUDE_CLI_TIMEOUT_S)}),
            (codex_cli, "_chat_codex_cli", "gpt-5.6-terra", ("rick", "gpt-5.6-terra", "sys", self.MESSAGES), {}),
        ]
        for module, name, model_id, args, kwargs in cases:
            with self.subTest(model=model_id), patch.object(module, name, return_value="ok") as run:
                llm_client.chat("rick", "sys", self.MESSAGES, model_id=model_id, max_tokens=7, effort="low")
                run.assert_called_once_with(*args, **kwargs)


if __name__ == "__main__":
    unittest.main()
