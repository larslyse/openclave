"""Running one model call (split out of llm_client.py): the per-run settings
and MCP servers a CLI call gets, stopping a running call, timeouts, and the
transcript and error text the CLI routes share."""
import contextlib
import os
import re
import signal
import sys
import threading
import uuid
from pathlib import Path

import agents
import llm_models


MCP_VAULT_SERVER = str(Path(__file__).parent / "mcp_vault_server.py")
# Agents currently in a look-up-only run (a reminder written at send time,
# reminders.py). Their HA admin server refuses anything but reads, and HA's
# own "assist" server (which switches devices) isn't loaded at all.
READ_ONLY_RUNS: set[str] = set()
# Extra env for the agent's own stdio MCP servers during one run, e.g. a
# scheduled task's pre-approved HA actions (scheduled_tasks.py).
RUN_ENV: dict[str, dict[str, str]] = {}


def _extra_servers(agent_id: str) -> dict:
    cfg = agents.AGENTS.get(agent_id)
    servers = _expand_server_env(cfg.mcp_servers) if cfg else {}
    for server in servers.values():
        server.pop("codex", None)
    if agent_id in READ_ONLY_RUNS:
        import ha_guard
        servers.pop("ha", None)
        if "ha-admin" in servers:
            admin = dict(servers["ha-admin"])
            admin["env"] = {**admin.get("env", {}), ha_guard.READ_ONLY_ENV: "1"}
            servers["ha-admin"] = admin
    for name, server in servers.items():
        if agent_id in RUN_ENV and "command" in server:
            servers[name] = {**server, "env": {**server.get("env", {}), **RUN_ENV[agent_id]}}
    return servers


def _cli_tools(agent_id: str) -> str:
    cfg = agents.AGENTS.get(agent_id)
    return ",".join(cfg.cli_tools) if cfg else ""


# Placeholders agent data can use for this install's own files, so an
# agent.json never hardcodes where the code lives.
_BUILTIN_PLACEHOLDERS = {"OPENCLAVE_DIR": str(Path(__file__).parent), "OPENCLAVE_PYTHON": sys.executable}


def _expand_server_env(value):
    """Expand ``${NAME}`` placeholders in agent-owned MCP server data:
    ``${OPENCLAVE_DIR}`` / ``${OPENCLAVE_PYTHON}``, else environment variables."""
    if isinstance(value, dict):
        return {key: _expand_server_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_server_env(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: _BUILTIN_PLACEHOLDERS.get(m.group(1)) or os.environ.get(m.group(1), ""), value)
    return value


# Slow (heavy) models doing real work, e.g. Cathie building a full financial
# model on GPT-6 Astra, can legitimately need more than 5 minutes (2026-10-02).
SLOW_MODEL_CLI_TIMEOUT_S = 600


def _cli_timeout(model_id: str, default: int) -> int:
    model = llm_models._BY_ID.get(model_id)
    return SLOW_MODEL_CLI_TIMEOUT_S if model and model.speed == "slow" else default


def _timeout_reply(route: str, model_id: str, timeout_s: int) -> str:
    name = llm_models.display_name(model_id) or model_id
    return (f"({route} error: no reply from {name} within {timeout_s // 60} minutes, so the run was stopped. "
            "Files it already wrote are kept. Try a faster model, or ask for the work in smaller steps.)")


def _cli_diagnostic(stderr: str, returncode: int | None) -> str:
    """The useful part of a failed CLI call's stderr. Codex echoes the whole
    prompt (system instructions, history, vault notes) to stderr before its own
    output, so a plain tail or head of stderr shows prompt text and cuts off
    the real cause ('ERROR: Selected mo…', R2 2026-10-02). Prefer the CLI's
    own ERROR lines; otherwise the last few short lines. Bounded, so prompts
    and notes never end up in the UI wholesale."""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    errors = [line for line in lines if re.match(r"(?i)^(error|fatal)\b[:\s]", line)]
    picked = list(dict.fromkeys(errors))[-3:] if errors else lines[-3:]
    detail = " | ".join(line[:200] for line in picked)[:500] or "no error output"
    return f"exit code {returncode}: {detail}" if returncode is not None else detail


# Stopping a reply mid-way (Rick's backlog, 2026-09-27). Each CLI call runs in
# its own process group with a unique OPENCLAVE_RUN marker in its env, so
# cancel_running can find and stop it (and Codex's sandbox children) without
# holding on to the Popen, which subprocess.run doesn't expose.
_RUN_MARKERS: dict[str, str] = {}


def _mark_run(agent_id: str, env: dict) -> str:
    marker = f"{agent_id}:{uuid.uuid4().hex}"
    env["OPENCLAVE_RUN"] = marker
    _RUN_MARKERS[agent_id] = marker
    return marker


def _unmark_run(agent_id: str, marker: str) -> None:
    if _RUN_MARKERS.get(agent_id) == marker:
        del _RUN_MARKERS[agent_id]


# API routes stream (request_phase text), so they can stop between chunks:
# agent id -> the running call's stop flag.
_API_STOPS: dict[str, threading.Event] = {}


class StoppedByOwner(RuntimeError):
    pass


@contextlib.contextmanager
def _api_run(agent_id: str):
    stop = threading.Event()
    _API_STOPS[agent_id] = stop
    try:
        yield stop
    finally:
        if _API_STOPS.get(agent_id) is stop:
            del _API_STOPS[agent_id]


def _check_stop(stop: threading.Event) -> None:
    if stop.is_set():
        raise StoppedByOwner("stopped by the user")


def cancel_running(agent_id: str) -> bool:
    """Stops the agent's running call, if any: a CLI process, or an API
    stream at its next chunk. False when nothing is running."""
    api_stop = _API_STOPS.get(agent_id)
    if api_stop:
        api_stop.set()
        return True
    marker = _RUN_MARKERS.get(agent_id)
    if not marker:
        return False
    needle = f"OPENCLAVE_RUN={marker}".encode()
    groups = set()
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            if needle in (proc_dir / "environ").read_bytes().split(b"\0"):
                groups.add(os.getpgid(int(proc_dir.name)))
        except OSError:
            continue
    for group in groups:
        try:
            os.killpg(group, signal.SIGTERM)
        except OSError:
            pass
    return bool(groups)


def _format_transcript(messages: list[dict]) -> str:
    # "### User"/"### Assistant" headers, not a bare "User: ...", "Assistant: ...".
    # Confirmed live (2026-09-16, Token Efficiency investigation): once retrieved
    # vault context started riding inside the message content instead of the
    # system prompt, a plain "User: <content>" label collided with claude -p's
    # own transcript-role heuristics whenever the content itself contained
    # "Label: value" lines (QMD dumps are full of them — Title:, Score:, Type:) —
    # the model would see only a fragment ("A:") instead of the real message. A
    # distinctive, markdown-header-style marker doesn't collide with plain
    # "word:" patterns in arbitrary retrieved content.
    lines = [f"### {'User' if m['role'] == 'user' else 'Assistant'}\n{m['content']}" for m in messages]
    lines.append("### Assistant")
    return "\n\n".join(lines)


def _stopped_reply(route: str, model_id: str, timeout_s: int, steps: list[str]) -> str:
    """A timed-out run. With finished steps it is a normal (partial) reply, so
    it lands in the transcript and the next turn can carry on from it; errors
    only live in the job record. Without steps it stays an error."""
    if not steps:
        return _timeout_reply(route, model_id, timeout_s)
    name = llm_models.display_name(model_id) or model_id
    return (f"**Stopped after {timeout_s // 60} minutes before {name} finished.** "
            "These steps were done; anything after them was not:\n"
            + "\n".join(f"- {s}" for s in steps)
            + "\n\nSay \"continue\" and I'll check what is in place and do the rest.")
