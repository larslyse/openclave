"""Shared, reusable LLM-call wrapper (spec §7) — generic across agents.

Pattern: single stateless call per turn. A minimal, agent-specific system prompt is
assembled by the caller and passed in; this module just makes the call, exposes a
handful of vault tools (writes scoped to the calling agent's own folder — plan,
backlog, mission, persona, notes, dashboard, tracking; reads whole-vault, matching
the read-anywhere permission model) and executes up to a few rounds of tool use.

Provider is per-agent, chosen via llm_models (backed by the dashboard's model
selector) — xAI (Grok) and Anthropic (Claude) are both wired; whichever the
selection resolves to at call time decides the client and request shape.
"""
import base64
import contextlib
import json
import os
import re
import subprocess
import signal
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

# anthropic/openai are deliberately NOT imported here — see _chat_anthropic,
# _chat_xai, and _summarize_transcript, which import them lazily. Measured
# live: importing both costs 1-2.5s. mcp_vault_server.py imports this module
# just for _run_tool, and is re-spawned fresh on every claude-cli/codex-cli
# chat message (most agents) — those subprocesses never call either provider
# client, so a top-level import here meant paying that cost on every single
# message for no reason. This was the single largest lever found while
# profiling response latency (2026-09-18).

import claude_auth
import config
import agents
import llm_models
import request_phase
import vault
from state_io import load_json, save_json
from llm_compaction import (  # noqa: F401 (re-exported)
    COMPACTION_CHAR_THRESHOLD, COMPACTION_KEEP_RECENT_TURNS, COMPACTION_MODEL,
    _summarize_transcript, maybe_compact_conversation,
)
from providers.codex_cli import (  # noqa: F401 (re-exported)
    CODEX_BIN, CODEX_CLI_TIMEOUT_S, CODEX_WRITE_GUIDANCE, _CODEX_PHASES, _chat_codex_cli, _toml_str,
    _watch_codex_phase, codex_phase_for, codex_steps_done,
)
from providers.claude_cli import (  # noqa: F401 (re-exported)
    CLAUDE_BIN, CLAUDE_CLI_TIMEOUT_S, _chat_claude_cli, _claude_stream_error, _split_images,
    _watch_claude_phase, claude_phase_for, claude_steps_done,
)
from providers.anthropic_api import (  # noqa: F401 (re-exported)
    _anthropic_content, _anthropic_rounds, _anthropic_tools, _chat_anthropic,
)
from providers.xai_api import (  # noqa: F401 (re-exported)
    XAI_BASE_URL, _chat_xai, _read_openai_stream,
)
from llm_tools import (  # noqa: F401 (re-exported)
    MAX_DASHBOARD_CHARS, MAX_TOOL_ROUNDS, READ_ONLY_TOOLS, SOFT_FILE_SIZE_LIMIT, _TOOLS_RUN,
    _has_web_search, _run_tool, _size_note, start_tool_log, tool_phase, tools_for, tools_run,
)
from llm_tool_defs import (  # noqa: F401 (re-exported)
    TOOLS,
)
from llm_runs import (  # noqa: F401 (re-exported)
    MCP_VAULT_SERVER, READ_ONLY_RUNS, RUN_ENV, SLOW_MODEL_CLI_TIMEOUT_S, StoppedByOwner, _API_STOPS,
    _BUILTIN_PLACEHOLDERS, _RUN_MARKERS, _api_run, _check_stop, _cli_diagnostic, _cli_timeout,
    _cli_tools, _expand_server_env, _extra_servers, _format_transcript, _mark_run, _stopped_reply,
    _timeout_reply, _unmark_run, cancel_running,
)
from llm_usage import (  # noqa: F401 (re-exported)
    CLI_QUOTA_FILE, USAGE_LOG, USAGE_WINDOWS, _log_codex_usage, _read_usage_entries,
    _record_codex_rate_limits, api_equivalent_usd, codex_rate_limits_from_log, codex_usage_from_log,
    log_usage, usage_summary,
)


def chat(agent_id: str, system_prompt: str, messages: list[dict], *, model_id: str | None = None, max_tokens: int | None = None, effort: str | None = None) -> str:
    """messages: list of {"role": "user"|"assistant", "content": str}. Returns the final reply text.

    model_id overrides the agent's configured selection for this one call —
    used by the voice path (chat_service.run_voice_reply_locked), which needs
    a fast direct-API model regardless of what the agent's text-chat model is
    set to (see Build Docs/Archive/voice.md: claude-opus-4-6 via claude-cli measured
    22-39s for a short reply, too slow for a live conversation).

    max_tokens overrides the default 8192 cap (anthropic provider only) —
    also used by the voice path: measured live 2026-09-21, a "real question"
    reply ran 500-600 chars despite the voice-mode prompt's "1-3 sentences"
    instruction, and since both LLM generation and TTS synthesis time scale
    with reply length, an unbounded reply inflates both halves of the voice
    pipeline (one real turn measured 24.6s in the LLM step alone). A hard cap
    is a more reliable backstop than prompt instructions proved to be.

    effort sets output_config.effort (anthropic provider only) — the voice path
    uses "low" so adaptive thinking can't eat its small max_tokens budget."""
    model = llm_models.get(model_id or llm_models.selected_model_id(agent_id))
    if model.provider == "claude-cli":
        # The timeout follows the catalog entry (its speed), not the API id it
        # runs under: claude-opus-5-5-cli is slow, claude-opus-5-5 is not, which
        # cut R2's big HA builds off at 5 minutes instead of 10 (2026-10-17).
        return _chat_claude_cli(agent_id, model.api_model_id or model.id, system_prompt, messages,
                                timeout_s=_cli_timeout(model.id, CLAUDE_CLI_TIMEOUT_S))
    if model.provider == "codex-cli":
        return _chat_codex_cli(agent_id, model.id, system_prompt, messages)
    if model.provider == "anthropic":
        return _chat_anthropic(agent_id, model.id, system_prompt, messages, max_tokens=max_tokens, effort=effort)
    return _chat_xai(
        agent_id, model.id, system_prompt, messages,
        api_model_id=model.api_model_id, reasoning_effort=model.reasoning_effort,
    )


