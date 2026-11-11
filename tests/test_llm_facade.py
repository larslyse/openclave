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
import vault

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


if __name__ == "__main__":
    unittest.main()
