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

XAI_BASE_URL = "https://api.x.ai/v1"
MAX_TOOL_ROUNDS = 5
MAX_DASHBOARD_CHARS = 100_000
CLAUDE_CLI_TIMEOUT_S = 300
# A long-running conversation resends its ENTIRE raw history on every turn
# (main.py's chat() builds `history` straight from vault.get_conversation()) —
# with no cap this grows every message's cost/latency forever and eventually
# breaks. Mirrors Hermes Agent's compaction design: before that gets out of
# hand, summarize everything except the most recent turns into one block, and
# archive the untouched original so nothing is silently lost (vault.py's
# compact_conversation). Threshold is on raw characters, not tokens — cheap
# to check on every chat call, conservative enough (~40k chars is roughly
# 10k tokens) to leave real headroom before any provider's context limit.
COMPACTION_CHAR_THRESHOLD = 40_000
COMPACTION_KEEP_RECENT_TURNS = 8
# Deliberately NOT whichever provider the agent itself is configured to use:
# compaction is background housekeeping, not a user-facing reply, so it
# shouldn't inherit claude-cli's per-message subprocess overhead or cost, or
# depend on the agent's own model choice at all.
COMPACTION_MODEL = "grok-4-1-fast-non-reasoning"
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


# Absolute path, not "claude": systemd services don't inherit an interactive
# shell's PATH, so a bare "claude" 404s under uvicorn even though it resolves fine
# from a login shell. This is the stable ~/.local/bin symlink, not the versioned
# target, so it keeps resolving correctly across `claude update`.
CLAUDE_BIN = config.CONFIG.claude_bin
# Same reasoning, same fix, for the OpenAI Codex CLI (subscription path via
# `codex exec` — see _chat_codex_cli).
CODEX_BIN = config.CONFIG.codex_bin
CODEX_CLI_TIMEOUT_S = 300
# Slow (heavy) models doing real work, e.g. Cathie building a full financial
# model on GPT-6 Astra, can legitimately need more than 5 minutes (2026-10-02).
SLOW_MODEL_CLI_TIMEOUT_S = 600


def _cli_timeout(model_id: str, default: int) -> int:
    model = llm_models._BY_ID.get(model_id)
    return SLOW_MODEL_CLI_TIMEOUT_S if model and model.speed == "slow" else default


def _claude_stream_error(output: str) -> str:
    """The error text Claude CLI wrote to its stream-json output, if any."""
    events = []
    for line in (output or "").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    for e in reversed(events):
        if e.get("type") == "result" and e.get("is_error") and (e.get("result") or e.get("errors")):
            return str(e.get("result") or e.get("errors"))[:500]
    for e in reversed(events):
        if e.get("type") == "assistant" and e.get("error"):
            text = " ".join(b.get("text", "") for b in (e.get("message") or {}).get("content", []) if isinstance(b, dict))
            return f"{e['error']}: {text}".strip(": ")[:500]
    return ""


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

# Token/cost tracking (Build Docs/Archive/Token Efficiency.md) — one JSON-lines log,
# not one file per agent, since "total spend across everything" is the
# top-line question and per-agent is just a filter over the same data.
# Ops state, not agent knowledge, so this lives under OPENCLAVE_HOME/state,
# not in the vault.
USAGE_LOG = config.CONFIG.state_path("usage_log.jsonl")


def log_usage(agent_id: str, provider: str, model_id: str, usage: dict, cost_usd: float | None) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": agent_id,
        "provider": provider,
        "model_id": model_id,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "cost_usd": cost_usd,
    }
    try:
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # usage logging must never break a chat reply


USAGE_WINDOWS = {"7d": 7, "30d": 30, "all": None}


def _read_usage_entries(agent_id: str | None) -> list[dict]:
    if not USAGE_LOG.exists():
        return []
    out = []
    with open(USAGE_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if agent_id is None or entry.get("agent_id") == agent_id:
                out.append(entry)
    return out


def api_equivalent_usd(entry: dict) -> float | None:
    """What a subscription (CLI) call would have cost at the same model's API
    list price. An estimate: cached input is counted at 10% of the input
    price, which is typical but varies by provider."""
    model = llm_models._BY_ID.get(entry.get("model_id"))
    price_in, price_out = llm_models.api_prices(model) if model else (None, None)
    if price_in is None or price_out is None:
        return None
    fresh_in = entry.get("input_tokens", 0) + entry.get("cache_creation_input_tokens", 0)
    cached = entry.get("cache_read_input_tokens", 0)
    return (fresh_in * price_in + cached * price_in * 0.1
            + entry.get("output_tokens", 0) * price_out) / 1_000_000


def usage_summary(agent_id: str | None = None) -> dict:
    """tokens in/out + cost, summed over 7d/30d/all-time windows. agent_id=None
    aggregates across every agent (for the root dashboard's total)."""
    entries = _read_usage_entries(agent_id)
    now = datetime.now(timezone.utc)
    windows = {}
    for name, days in USAGE_WINDOWS.items():
        tokens_in = tokens_out = 0
        cost = 0.0
        has_cost = False
        equiv = 0.0
        has_equiv = False
        for e in entries:
            if days is not None:
                try:
                    ts = datetime.fromisoformat(e["ts"])
                except (KeyError, ValueError):
                    continue
                if (now - ts).days >= days:
                    continue
            tokens_in += e.get("input_tokens", 0) + e.get("cache_creation_input_tokens", 0) + e.get("cache_read_input_tokens", 0)
            tokens_out += e.get("output_tokens", 0)
            if e.get("cost_usd") is not None:
                cost += e["cost_usd"]
                has_cost = True
            else:
                price = api_equivalent_usd(e)
                if price is not None:
                    equiv += price
                    has_equiv = True
        windows[name] = {"tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost if has_cost else None,
                         "api_equiv_usd": equiv if has_equiv else None}
    return windows


# Codex has no single "disable every built-in tool" switch like Claude CLI's
# `--tools ""`. `--disable <feature>` looked like the closest thing, but
# confirmed live (bisected all 7 candidates individually — shell_tool,
# unified_exec, browser_use[_external/_full_cdp_access], computer_use,
# image_generation) that passing *any* of them breaks MCP tool access
# entirely for this session, for reasons not worth chasing further — not
# used here as a result. A bare, unremovable `functions.exec` (plus
# apply_patch, and a `collaboration.*` multi-agent tool family) is available
# regardless of what gets disabled anyway, so there was never much real
# lockdown value being traded away. The actual boundary is the
# `-s workspace-write` sandbox in _chat_codex_cli, confirmed live to
# actually contain what `functions.exec`/apply_patch can touch.

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "update_backlog",
            "description": "Add a new open item to the agent's backlog, or mark an existing item done/not done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "check", "uncheck"]},
                    "item": {"type": "string", "description": "The backlog item text (for 'add'), or a substring identifying an existing item (for 'check'/'uncheck')."},
                },
                "required": ["action", "item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan_phase",
            "description": "Update the completion percent and/or status of a phase in the current plan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phase": {"type": "string", "description": "Phase name."},
                    "percent": {"type": "integer", "minimum": 0, "maximum": 100},
                    "status": {"type": "string", "enum": ["not-started", "in-progress", "complete"]},
                },
                "required": ["phase", "percent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_mission",
            "description": (
                "Replace your 'mission.md' body — the living baseline of who the user is and what you're "
                "tracking for them (shown to you above as '# Mission'). Use this whenever conversation "
                "reveals something that should update your standing context (a changed constraint, a "
                "new goal, a corrected fact) rather than just living in this session's chat log. Write "
                "the full new body — keep sections that are still true, don't just append. This file "
                "also has a '## Domains' bullet list that other agents see as your expertise tags — "
                "keep that section intact (or update it deliberately, not by accident) when you rewrite "
                "the body. Frontmatter is preserved automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "body": {"type": "string", "description": "Full replacement markdown body (no frontmatter)."},
                },
                "required": ["body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_persona",
            "description": (
                "Replace your own persona.md body — who you are and your tone (shown to you above, "
                "before '# Mission'). Use this rarely: only when the user explicitly asks you to change how "
                "you operate, or you've found a genuinely better way to describe your own lane. Don't "
                "use it to expand your own scope unprompted, and mention in your reply that you changed "
                "it so the user notices. Frontmatter is preserved automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "body": {"type": "string", "description": "Full replacement markdown body (no frontmatter)."},
                },
                "required": ["body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_feedback",
            "description": (
                "Log one specific correction or confirmation about HOW you interact with the user — not a "
                "domain fact (that's update_mission) and not a full personality rewrite (that's "
                "update_persona), but a short durable rule appended to your own persona.md under "
                "'## Feedback'. Use this whenever the user corrects your approach ('stop doing X', 'don't add "
                "Y') or clearly confirms something worked well without being asked — quiet confirmations "
                "count too, not just corrections. Don't wait to be asked; log it right when it happens."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entry": {"type": "string", "description": "One short, self-contained rule, e.g. 'Give the number first on routine metric checks, add commentary only if asked — the user checks these mid-task and wants the fact, not framing.'"},
                },
                "required": ["entry"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_vault",
            "description": (
                "Keyword-search the owner's whole Obsidian vault for anything not already covered by the "
                "'Retrieved context' section above. Use this whenever the user references something "
                "specific you don't have, asks you to look something up, says they added new info, "
                "or a question needs vault content beyond your mission/plan/backlog. "
                "Every word of a search must match, so keep each alternative to 1-3 words. "
                + vault.search_language_hint()
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keywords, short and specific; alternatives separated by |."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_vault_file",
            "description": "Read the full contents of one specific vault file using an absolute vault path from your context directory or a qmd:// URI from search_vault.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute vault file path or qmd:// URI."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_memory",
            "description": (
                "Save a freeform entry in your own memory/ folder — the LOWEST-priority bucket, for "
                "genuinely one-off things that don't need to be in every future context window (a "
                "passing thought, a summary of something you looked into). Not auto-loaded into future "
                "chats — use list_memories / read_memory to pull one back up when it's relevant. "
                "Do NOT use this for a durable protocol/equipment fact that should shape every future "
                "recommendation — that's update_mission. Do NOT use this for a dated qualitative "
                "dated qualitative information — that's log_measurement in a section from your "
                "tracking.md, so it stays in the readable trend record instead of an unindexed folder."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short title — used to generate the note's filename."},
                    "body": {"type": "string", "description": "Note content, markdown."},
                },
                "required": ["title", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_memories",
            "description": "List your own saved memory entries (title + date), most recent first.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_memory",
            "description": "Read the full content of one of your own memory entries, by id (from list_memories).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string"},
                },
                "required": ["entry_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_measurement",
            "description": (
                "Record one dated measurement, activity, or qualitative note in your tracking log. "
                "This is the home "
                "for ANYTHING dated that's worth watching over time, whether it's a number or free text: "
                "if forgetting it by next session would change the advice you give, and it's tied to a "
                "specific date rather than being a standing fact, it belongs here, not in memory/. Call "
                "get_measurements with no metric filter first if you're unsure what metric names are "
                "already in use, so you reuse the same name consistently instead of a near-duplicate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "description": "Short, consistent name for the value or topic being tracked."},
                    "value": {"type": "string", "description": "The recorded value. Leave empty for a note without a single value."},
                    "unit": {"type": "string", "description": "Optional unit for the value."},
                    "note": {"type": "string", "description": "Free-text context for this entry, or the full content for a note-only entry."},
                    "date": {"type": "string", "description": "Optional date (YYYY-MM-DD). Defaults to today if omitted."},
                    "section": {
                        "type": "string",
                        "description": (
                            "Section heading from your tracking.md file. Reuse an existing heading; "
                            "if the file has no sections yet, use 'Progress'."
                        ),
                    },
                },
                "required": ["metric"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_measurements",
            "description": (
                "Read back entries from one section in your tracking.md, most recent first. Omit metric "
                "to see every metric in that section; pass metric to filter to one trend."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "description": "Optional metric name filter."},
                    "limit": {"type": "integer", "description": "Optional max number of entries to return."},
                    "section": {"type": "string", "description": "A section heading from your tracking.md; defaults to Progress."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_foundation",
            "description": "Record a durable decision basis in optional foundations.md. Use kind=claim for evidence-based knowledge, with source and uncertainty; use kind=operating_rule for agreed rules, with rationale and no confidence score. Read get_foundations first. Do not invent entries or create filler just to populate the file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Topic section, or general."},
                    "foundation": {"type": "string", "description": "Concise claim or agreed operating rule. State limitations or uncertainty where relevant."},
                    "kind": {"type": "string", "enum": ["claim", "operating_rule"]},
                    "reference": {"type": "string", "description": "Source and evidence limitations for a claim; agreement context and rationale for an operating rule."},
                    "confidence": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Optional confidence for claims only. Omit for operating rules."},
                },
                "required": ["topic", "foundation", "kind", "reference"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_foundations",
            "description": "Read back recorded foundations — one topic if given, or every topic concatenated if omitted. Use before add_foundation to avoid duplicates, or when a recommendation needs to cite what it's grounded in.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Optional: one sub-topic section, e.g. 'foot'. Omit to read everything."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_own_dashboard",
            "description": "Read the current HTML of your own dashboard page (what's shown at your own /<agent> URL), so you can edit it incrementally instead of guessing its current state.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_own_dashboard",
            "description": (
                "Replace your own dashboard page with new HTML. This is YOUR page — you decide what's "
                "useful to show given what you're currently working on with the user (plan, backlog, "
                "conversation history, or a different layout entirely). It's a single static file: inline "
                "your own <style> and <script>, no build step or restart needed — changes are live the "
                "next time the page loads. JSON endpoints your own JS can fetch from (replace <agent> "
                "with your own agent id): GET /<agent>/plan, GET /<agent>/backlog, "
                "GET /<agent>/tracking (optional ?metric=&limit= query params), "
                "POST /<agent>/tracking (body: {metric, value?, unit?, note?, date?} — writes straight to "
                "the tracking log with no LLM call, so it's the right target for quick-log buttons: instant "
                "and free, unlike routing a button press through /chat), "
                "GET /<agent>/conversations, GET /<agent>/conversations/{id}, POST /<agent>/chat "
                "(body: {message, conversation_id}). The previous version is automatically backed up "
                "before being overwritten. "
                "Your page runs sandboxed (no access to other agents' pages or endpoints — plain "
                "fetch('/<agent>/...') calls to your OWN endpoints work exactly as shown above, nothing to "
                "change there). localStorage/sessionStorage work but don't persist across a reload; don't "
                "rely on them for anything that needs to survive that. "
                "Shared chat/conversation-list/backlog/kanban/usage/markdown-rendering behavior lives in "
                "/static/dashboard_common.css and /static/dashboard_common.js — link/script-src those two "
                "rather than pasting your own copies (read_own_dashboard shows the current pattern: a small "
                "<style> block with your color variables, the HTML skeleton, then those two includes, then "
                "only your own genuinely custom panels). Keeps every dashboard's shared code fixable in one "
                "place instead of seven."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "html": {"type": "string", "description": "Full HTML document to save as the new dashboard page."},
                },
                "required": ["html"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_note",
            "description": (
                "Create or update a plain note file inside your own folder — for structured, ongoing "
                "content that doesn't fit your other tools (a per-project file, an inventory list, working "
                "notes). Path is relative to your own folder root and must end in .md or .txt. Reserved "
                "paths (dashboard.html, backlog.md, plan.md, mission.md, persona.md, foundations.md, "
                "tracking.md, conversations/, memory/) are refused — those already have dedicated tools. "
                "To update a note that already exists, call read_note first and pass back its version, so "
                "a stale rewrite can't silently clobber a change you haven't seen."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside your own folder, e.g. 'projects/motor-control.md'."},
                    "content": {"type": "string", "description": "Full new content of the note."},
                    "expected_version": {"type": "string", "description": "Version string from the last read_note/write_note of this path. Omit only when creating a brand-new file."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_note",
            "description": "Read one of your own note files by path (from list_notes or a path you already know). Returns a version marker to pass back to write_note when updating it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside your own folder."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_notes",
            "description": "List your own note files created via write_note, most recently modified first.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clave_backlog",
            "description": (
                "Your clave's shared backlog (work that spans its agents). read it, add an item, or (the lead "
                "only, when there is one) check/uncheck an item by part of its text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "add", "check", "uncheck"]},
                    "item": {"type": "string", "description": "Item text for add, or part of it for check/uncheck."},
                    "clave": {"type": "string", "description": "Clave id; only needed if you are in more than one."},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_vault_note",
            "description": (
                "Write in the vault folders the owner gave you beyond your own folder (listed in your "
                "instructions; refused anywhere else). mode 'append' adds to an existing note, 'create' makes "
                "a new note in an existing folder. Never deletes. Search first and prefer appending."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Vault-relative note path, e.g. 'Area/Blog/Post title.md'."},
                    "content": {"type": "string", "description": "The Markdown to append, or the whole new note."},
                    "mode": {"type": "string", "enum": ["append", "create"]},
                    "change_summary": {"type": "string", "description": "One line: what changed and why."},
                },
                "required": ["path", "content", "mode", "change_summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_pdf",
            "description": (
                "Make a print-ready A4 PDF of a note anywhere in the user's vault, saved next to the note with "
                "the same name (.pdf); it syncs wherever the user's notes sync. Use it when the user asks for a "
                "PDF. Figures stay with their captions, tables and short sections are kept on one page, "
                "headings never end a page. To force a new page, put <div class=\"page-break\"></div> on "
                "its own line in the note (edit the note first if the user wants that); an italic line right "
                "after an image becomes its caption. Notes in configured confidential folders are never exported."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "The note: its vault path, a path or qmd:// URI from search_vault, or just its name."},
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "message_agent",
            "description": (
                "Send another OpenClave agent a message: a question in their domain, or data they should "
                "keep (a purchase to log, a measurement, a finding). It lands in their 'From <you>' "
                "conversation, they act on it and answer there, and the user can read it. You don't get the "
                "answer back unless they message you. Put everything they need in the message itself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "The recipient's name from the agent roster."},
                    "message": {"type": "string", "description": "The full message, with any data they need."},
                },
                "required": ["agent", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flag_to_maintainer",
            "description": (
                "Send an improvement for the OpenClave app itself to the maintainer: a bug, a broken "
                "or missing tool, or a feature idea. The one deliberate exception to only writing inside your "
                "own folder. Not for research, lookups or domain questions: do those yourself (you have web "
                "search). Returns an item id; use check_maintainer_request with it later if the outcome matters."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "A short, specific description of the bug or improvement."},
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_maintainer_request",
            "description": "Check the status/result of something previously flagged to the maintainer via flag_to_maintainer, using the id it returned. Status is 'open' (not yet looked at), 'acknowledged' (seen, no result yet), or 'resolved' (result field has the answer).",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string", "description": "The id returned by flag_to_maintainer."},
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_maintainer_inbox_item",
            "description": (
                "Maintainer-only: acknowledge or resolve an inbox item. Use 'acknowledged' once you've seen it "
                "and are working it, 'resolved' once done — pass result with the answer/outcome so the "
                "flagging agent can retrieve it via check_maintainer_request without the user relaying it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string", "description": "The inbox item's id."},
                    "status": {"type": "string", "enum": ["acknowledged", "resolved"], "description": "New status."},
                    "result": {"type": "string", "description": "Answer/outcome to store, typically set together with status='resolved'."},
                },
                "required": ["item_id"],
            },
        },
    },
]

# The manager's proposals (proposals.py): it asks before it acts.
TOOLS.append({
    "type": "function",
    "function": {
        "name": "propose_to_owner",
        "description": (
            "Manager only. Propose something you want to do, for the user to answer Yes / No / Discuss on your "
            "page; nothing happens until they say yes. kind=vault_note: write something worth keeping into the "
            "main vault (target = the note's vault path from search_vault; mode=append to an existing note, the "
            "default, or create for a new note in an existing folder; content = the exact Markdown to add, "
            "distilled, with a source line; source = the agent file it came from). kind=message_agent: a message "
            "to another agent (agent = its name, content = the whole message). kind=task: anything else you'd do "
            "yourself (content = exactly what); on yes you're told to go ahead in your Audit conversation. Agent "
            "folders and confidential folders are never written."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["vault_note", "message_agent", "task"]},
                "title": {"type": "string", "description": "A few words, shown as the card's title."},
                "why": {"type": "string", "description": "One or two sentences: why the user would want it."},
                "content": {"type": "string", "description": "The note text, the message, or the task."},
                "target": {"type": "string", "description": "vault_note: the note's path inside the vault."},
                "mode": {"type": "string", "enum": ["append", "create"], "description": "vault_note only."},
                "agent": {"type": "string", "description": "message_agent: the recipient's name."},
                "source": {"type": "string", "description": "vault_note: where it came from (agent file path)."},
            },
            "required": ["kind", "title", "why", "content"],
        },
    },
})

# Skills (skills.py): know-how loaded on demand; the prompt lists only names.
TOOLS.append({
    "type": "function",
    "function": {
        "name": "read_skill",
        "description": (
            "Load one of your skills (listed under Skills in your instructions) before doing a job it covers, "
            "then follow it. file= reads another file that comes with the skill."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The skill's name from the list."},
                "file": {"type": "string", "description": "Optional: a file inside the skill's folder."},
            },
            "required": ["name"],
        },
    },
})

TOOLS.append({
    "type": "function",
    "function": {
        "name": "save_skill",
        "description": (
            "Write down how to do a recurring job as one of your own skills, so next time you (or a scheduled "
            "task) can follow it: after a job went well, or when the user explains how they want something done. "
            "Saving under an existing name of yours rewrites it; read it first and keep what still holds."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short lowercase name, e.g. 'ha-update'."},
                "description": {"type": "string", "description": "One sentence: what it's for and when to use it."},
                "instructions": {"type": "string", "description": "The steps and rules, in Markdown."},
            },
            "required": ["name", "description", "instructions"],
        },
    },
})

# The agent's own mobile page (custom_pages.py). check_page is read-only so
# it works on Codex too, where the agent writes page.json with its own tools.
TOOLS.append({
    "type": "function",
    "function": {
        "name": "check_page",
        "description": (
            "Your own page in the user's phone app (the button left of your name): shows the page format "
            "and what the user currently sees, with any errors. Call it before building or changing your "
            "page, and again after writing page.json."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
})

TOOLS.append({
    "type": "function",
    "function": {
        "name": "write_page",
        "description": (
            "Save your own page for the user's phone app (page.json in your folder; call check_page first "
            "for the format). It's checked before saving and refused with the reason if a block is wrong. "
            "Build it from your own files so it stays current when you update them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "page": {"type": "object", "description": "{\"title\": ..., \"blocks\": [...]} as check_page describes."},
                "html": {"type": "string", "description": "Optional: full HTML saved as page.html, for an html block."},
            },
            "required": ["page"],
        },
    },
})

TOOLS.append({
    "type": "function",
    "function": {
        "name": "restore_page",
        "description": (
            "Put an earlier version of your page back, e.g. when the user says \"revert your page\". "
            "Afterwards tell the user which version is back and ask before changing it again."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["phone", "desktop"],
                         "description": "phone = page.json (default); desktop = dashboard.html."},
                "version": {"type": "integer", "description": "From check_page's version list. Leave out for the last "
                            "phone version the user kept (desktop: the one before now)."},
            },
        },
    },
})

TOOLS.append({
    "type": "function",
    "function": {
        "name": "finish_onboarding",
        "description": (
            "Onboarding only, and only after the user has confirmed your proposal: saves which optional "
            "tools you get and ends onboarding. Write your mission.md and persona.md first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tools": {"type": "array", "items": {"type": "string"},
                          "description": "Optional tool names the user agreed to. Core tools are always included; don't list them."},
                "tagline": {"type": "string", "description": "Optional improved one-line description for the dashboard (max 60 chars)."},
                "avatar_svg": {"type": "string", "description": "Optional avatar the user agreed to: one small SVG, viewBox 0 0 64 64, plain shapes, under 20 KB."},
                "topics": {"type": "array", "items": {"type": "string"},
                           "description": "Chat topics the user agreed to (usually one per domain, short names). Created empty, in this order."},
            },
            "required": ["tools"],
        },
    },
})

TOOLS.append({
    "type": "function",
    "function": {
        "name": "find_voice",
        "description": (
            "Search Fish Audio's voice library for a voice for you to speak with in voice mode. Returns "
            "a few voices with id, description and a sample link. Give the user 2-3 that fit, each with its "
            "sample link so they can listen, and let them pick; then set_voice."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for, e.g. 'calm deep male', 'warm british female', 'energetic young'."},
                "language": {"type": "string", "description": "Voice language code, default 'en'."},
            },
            "required": ["query"],
        },
    },
})

TOOLS.append({
    "type": "function",
    "function": {
        "name": "set_voice",
        "description": "Use this Fish Audio voice for your voice-mode replies from now on. Only the one the user picked.",
        "parameters": {
            "type": "object",
            "properties": {"voice_id": {"type": "string", "description": "The id from find_voice."}},
            "required": ["voice_id"],
        },
    },
})

TOOLS.append({
    "type": "function",
    "function": {
        "name": "schedule_reminder",
        "description": (
            "Send the user a message later: a push notification to their phone, also posted in your "
            "'Reminders' conversation, where they can reply. For reminders, follow-ups on a future event, "
            "and recurring notes (e.g. a weekly watering note). By default the message is sent exactly as "
            "you write it now, so make it self-contained. With compose=true, message is instead an instruction "
            "you'll follow when it's due (e.g. 'check tomorrow's rain forecast for Oslo; tell the user whether to "
            "water the tomatoes'): you then run once, may only look things up, and your reply is what they get. "
            "Use compose only when the message depends on information you can't have now. "
            f"Times are local time ({config.CONFIG.timezone}); the current "
            "time is in each message's 'Now' line. Schedule only what the user asked for or agreed to. "
            "action='list' shows what's scheduled (with ids); action='cancel' removes one by id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "list", "cancel"]},
                "message": {"type": "string", "description": "add: the text the user receives (under 1000 characters); with compose=true, your instruction for then."},
                "compose": {"type": "boolean", "description": "add: true = write the message when it's due, from the instruction in message."},
                "at": {"type": "string", "description": "add: when, as 'YYYY-MM-DD HH:MM' local time; for a repeat, the first time."},
                "repeat": {"type": "string", "enum": ["daily", "weekly", "monthly"], "description": "add: leave out for a one-off."},
                "reminder_id": {"type": "string", "description": "cancel: the id from add or list."},
            },
            "required": ["action"],
        },
    },
})


CLI_QUOTA_FILE = config.CONFIG.state_path("cli_quota.json")


def codex_rate_limits_from_log(text: str) -> dict | None:
    """The last rate_limits object in a Codex session log (JSONL)."""
    latest = None
    for line in text.splitlines():
        if '"rate_limits"' in line:
            latest = line
    if latest is None:
        return None
    stack = [json.loads(latest)]
    while stack:  # rate_limits sits a few levels deep; don't hardcode the path
        node = stack.pop()
        if isinstance(node, dict):
            if isinstance(node.get("rate_limits"), dict):
                return node["rate_limits"]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def codex_usage_from_log(text: str) -> dict | None:
    """Token totals for the whole Codex run: the last token_count event's
    cumulative total_token_usage, in log_usage's field names."""
    latest = None
    for line in text.splitlines():
        if '"token_count"' in line and '"total_token_usage"' in line:
            latest = line
    if latest is None:
        return None
    try:
        total = json.loads(latest)["payload"]["info"]["total_token_usage"]
    except (ValueError, KeyError, TypeError):
        return None
    cached = int(total.get("cached_input_tokens") or 0)
    return {
        "input_tokens": max(0, int(total.get("input_tokens") or 0) - cached),
        "cache_read_input_tokens": cached,
        "output_tokens": int(total.get("output_tokens") or 0),
    }


def _log_codex_usage(agent_id: str, model_id: str, codex_home: Path) -> None:
    """The Codex route never logged usage, so Token usage looked empty with
    every agent on Codex (evaluation 2026-09-27). Never raises."""
    try:
        for session in codex_home.glob("sessions/**/*.jsonl"):
            usage = codex_usage_from_log(session.read_text(encoding="utf-8"))
            if usage:
                log_usage(agent_id, "codex-cli", model_id, usage, None)  # subscription: no $ figure
                return
    except Exception:
        pass


# What Codex is doing right now, from the session log it writes while it
# runs, so the chat can say "Searching the web" instead of "Thinking" for the
# whole reply. Matched on the raw log line; first match wins.
_CODEX_PHASES = (
    (("web__", "web_search"), "searching the web"),
    (("update_backlog", "update_plan_phase"), "updating plans"),
    (("make_pdf",), "making a pdf"),
    (("apply_patch", "write_file", "file_change", "write_note", "write_vault_note", "write_memory", "log_measurement"), "writing files"),
    (("search_vault", "read_vault_file", "read_note", "list_notes", "read_memory", "list_memories",
      "get_foundations", "get_measurements", "read_own_dashboard", "exec_command", "shell"), "reading notes"),
)


def tool_phase(name: str, arguments: dict | None = None) -> str:
    """A short activity label from a tool name, without exposing its inputs."""
    arguments = arguments if isinstance(arguments, dict) else {}
    if name.endswith("ha_api") or name == "ha_api":
        method = str(arguments.get("method", "")).upper()
        path = str(arguments.get("path", ""))
        return "reading home assistant" if method == "GET" or path == "/api/template" else "updating home assistant"
    if name.endswith("ha_ws") or name == "ha_ws":
        command = arguments.get("message") or {}
        kind = str(command.get("type", "")) if isinstance(command, dict) else ""
        return "reading home assistant" if kind.endswith(("/get", "/list")) else "updating home assistant"
    if "__ha__" in name or name == "assist":
        return "using home assistant"
    if "web_search" in name.lower() or name in {"WebSearch", "WebFetch"}:
        return "searching the web"
    if any(key in name for key in ("update_backlog", "update_plan_phase", "clave_backlog")):
        return "updating plans"
    if "make_pdf" in name:
        return "making a pdf"
    if any(key in name for key in ("search_vault", "read_vault_file", "read_note", "list_notes", "read_memory", "list_memories", "get_foundations", "get_measurements")):
        return "reading notes"
    if any(key in name for key in ("write_file", "file_change", "write_note", "write_vault_note", "write_memory", "log_measurement", "apply_patch")):
        return "writing files"
    return "using tools"


def codex_phase_for(line: str) -> str | None:
    if '"reasoning"' in line or ('"message"' in line and '"assistant"' in line):
        return "thinking"
    if not any(k in line for k in ('"custom_tool_call"', '"function_call"', '"mcp_tool_call"', '"web_search_call"', '"local_shell_call"')):
        return None
    try:
        payload = json.loads(line).get("payload", {})
        if isinstance(payload, dict):
            name = str(payload.get("name") or payload.get("tool") or "")
            server = str(payload.get("server") or "")
            if server and "ha" in server:
                name = f"mcp__{server}__{name}"
            arguments = payload.get("arguments") or {}
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if name and ("ha_api" in name or "ha_ws" in name or "__ha__" in name):
                return tool_phase(name, arguments)
    except (ValueError, TypeError, AttributeError):
        pass
    for needles, phase in _CODEX_PHASES:
        if any(n in line for n in needles):
            return phase
    return "using tools"


def codex_steps_done(codex_home: Path, limit: int = 40) -> list[str]:
    """Like claude_steps_done, from a Codex run's session log."""
    calls, done = {}, []
    for session in sorted(codex_home.glob("sessions/**/*.jsonl")):
        try:
            lines = session.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                payload = json.loads(line).get("payload") or {}
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type") or ""
            if kind in ("function_call", "custom_tool_call", "mcp_tool_call"):
                calls[payload.get("call_id")] = payload
            elif kind.endswith("_output") and payload.get("call_id") in calls:
                call = calls.pop(payload["call_id"])
                args = call.get("arguments") or call.get("input") or ""
                args = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
                args = " ".join(args.split())
                done.append(f"{call.get('name') or 'tool'} {args[:160]}{'…' if len(args) > 160 else ''}")
    if len(done) > limit:
        done = [f"… {len(done) - limit} earlier steps"] + done[-limit:]
    return done


def _watch_codex_phase(agent_id: str, codex_home: Path, stop: threading.Event) -> None:
    offsets: dict[Path, int] = {}
    while not stop.wait(0.4):
        try:
            for session in codex_home.glob("sessions/**/*.jsonl"):
                with open(session, "rb") as f:
                    f.seek(offsets.get(session, 0))
                    chunk = f.read()
                end = chunk.rfind(b"\n") + 1  # only whole lines
                offsets[session] = offsets.get(session, 0) + end
                phase = None
                for line in chunk[:end].decode("utf-8", "replace").splitlines():
                    phase = codex_phase_for(line) or phase
                if phase:
                    request_phase.set_phase(agent_id, phase)
        except Exception:
            pass  # cosmetic only


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


def _record_codex_rate_limits(codex_home: Path) -> None:
    """Keep the latest Codex subscription rate-limit reading for the Control
    Panel. Codex logs it in the session file under this call's temporary
    CODEX_HOME, which is deleted right after, so it's read here. Never raises."""
    try:
        found = None
        for session in codex_home.glob("sessions/**/*.jsonl"):
            found = codex_rate_limits_from_log(session.read_text(encoding="utf-8")) or found
        if not found:
            return
        data = load_json(CLI_QUOTA_FILE, {})
        data["codex"] = {**found, "observed_at": datetime.now(timezone.utc).isoformat()}
        save_json(CLI_QUOTA_FILE, data)
    except Exception:
        pass  # quota display is informational; never break a chat reply


def _has_web_search(agent_id: str) -> bool:
    import agent_tools  # lazy, like tools_for: agent_tools -> agents at import time
    return agent_tools.has_web_search(agent_id)


def tools_for(agent_id: str) -> list[dict]:
    """Per-agent subset of TOOLS (see agent_tools.py)."""
    import copy
    import agent_tools
    allowed = agent_tools.allowed(agent_id)
    tools = [copy.deepcopy(t) for t in TOOLS if t["function"]["name"] in allowed]
    import vault
    headings = ", ".join(repr(name) for name in vault.tracking_sections(agent_id))
    for tool in tools:
        if tool["function"]["name"] in {"log_measurement", "get_measurements"}:
            tool["function"]["parameters"]["properties"]["section"]["description"] = (
                f"Section heading from your tracking.md. Existing headings: {headings}. "
                f"Defaults to {vault.tracking_sections(agent_id)[0]!r}."
            )
    return tools


# Soft, not enforced: openclave.md already tells agents to keep mission/
# foundations/feedback "short enough to skim in a minute" and prune rather than
# let it grow — but until now nothing actually checked that, so it depended
# entirely on the model remembering. This surfaces a warning in the tool's
# own return value (which the model sees immediately) instead of silently
# trusting good behavior. Roughly Hermes Agent's always-loaded-memory cap
# (3,575 chars) as a reference point for "should still be skimmable."
SOFT_FILE_SIZE_LIMIT = 4000


def _size_note(path: Path) -> str:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return ""
    if size <= SOFT_FILE_SIZE_LIMIT:
        return ""
    return f" (note: {path.name} is now {size} chars — over the ~{SOFT_FILE_SIZE_LIMIT}-char soft limit for always-loaded context; consider pruning stale content)"



# Tools run during the current call, per agent (chat_service's check that a
# reply saying "Added" was backed by a tool, Rick's backlog 2026-10-13).
_TOOLS_RUN: dict[str, list[str]] = {}
READ_ONLY_TOOLS = frozenset({
    "search_vault", "read_vault_file", "list_memories", "read_memory", "get_measurements",
    "get_foundations", "read_own_dashboard", "read_note", "list_notes", "check_maintainer_request", "check_page",
    "read_skill",
})


def start_tool_log(agent_id: str) -> None:
    _TOOLS_RUN[agent_id] = []


def tools_run(agent_id: str) -> list[str]:
    return list(_TOOLS_RUN.get(agent_id, []))


def _run_tool(agent_id: str, name: str, tool_input: dict) -> str:
    _TOOLS_RUN.setdefault(agent_id, []).append(name)
    import agent_tools
    if name not in agent_tools.allowed(agent_id):
        return f"refused: {name} is not one of your tools"
    request_phase.set_phase(agent_id, tool_phase(name, tool_input))
    if name == "find_voice":
        import agent_voice
        try:
            found = agent_voice.search(tool_input.get("query", ""), tool_input.get("language") or "en")
        except agent_voice.VoiceError as exc:
            return f"refused: {exc}"
        if not found:
            return "no voices found; try other words (e.g. 'deep male', 'warm female', 'british')"
        return "\n".join(f"- {v['name']} (id {v['id']}): {v['description']} Tags: {', '.join(v['tags'])}. "
                         f"Sample: {v['sample'] or 'none'}" for v in found)
    if name == "set_voice":
        import agent_voice
        try:
            voice = agent_voice.set_voice(agent_id, tool_input.get("voice_id", ""))
        except agent_voice.VoiceError as exc:
            return f"refused: {exc}"
        return f"voice set: {voice['name']}. Your next voice-mode reply uses it (Norwegian replies keep the Norwegian voice)."
    if name == "finish_onboarding":
        import agent_factory
        return agent_factory.finish_onboarding(agent_id, tool_input.get("tools") or [], tool_input.get("tagline"),
                                               tool_input.get("avatar_svg"), tool_input.get("topics"))
    if name == "update_backlog":
        action = tool_input.get("action")
        item = tool_input.get("item", "")
        if action == "add":
            vault.backlog_add(agent_id, item)
            return f"added: {item}"
        if action in ("check", "uncheck"):
            changed = vault.backlog_set_done(agent_id, item, done=(action == "check"))
            return "updated" if changed else "no matching backlog item found"
        return f"unknown action: {action}"
    if name == "update_plan_phase":
        vault.update_plan_phase(
            agent_id, tool_input["phase"], tool_input.get("percent", 0), tool_input.get("status")
        )
        return "plan updated"
    if name == "update_mission":
        try:
            vault.update_mission(agent_id, tool_input.get("body", ""))
        except vault.IdentityLimitReached as exc:
            return str(exc)
        return "mission updated" + _size_note(vault.mission_file(agent_id))
    if name == "update_persona":
        try:
            vault.update_persona(agent_id, tool_input.get("body", ""))
        except vault.IdentityLimitReached as exc:
            return str(exc)
        return "persona updated" + _size_note(vault.persona_file(agent_id))
    if name == "add_feedback":
        try:
            vault.add_feedback(agent_id, tool_input.get("entry", ""))
        except vault.IdentityLimitReached as exc:
            return str(exc)
        return "feedback logged" + _size_note(vault.persona_file(agent_id))
    if name == "write_memory":
        entry_id = vault.write_memory(agent_id, tool_input.get("title", "untitled"), tool_input.get("body", ""))
        return f"memory saved: {entry_id}"
    if name == "list_memories":
        entries = vault.list_memory(agent_id)
        if not entries:
            return "(no memory entries yet)"
        return "\n".join(f"{e['id']}: {e['title']} ({e['date']})" for e in entries)
    if name == "read_memory":
        body = vault.read_memory(agent_id, tool_input.get("entry_id", ""))
        return body if body is not None else "memory entry not found"
    if name == "search_vault":
        if not vault.qmd_available():
            return f"(vault search is off — qmd executable '{vault.QMD_BIN}' was not found)"
        result = vault.search_vault(tool_input.get("query", ""))
        return result if result else "no results found. Try other words, or the same words in another language."
    if name == "read_vault_file":
        return vault.qmd_get(tool_input.get("path", ""))
    if name == "log_measurement":
        vault.log_measurement(
            agent_id,
            tool_input.get("metric", ""),
            tool_input.get("value"),
            tool_input.get("unit"),
            tool_input.get("note"),
            tool_input.get("date"),
            tool_input.get("section") or vault.DEFAULT_TRACKING_SECTION,
        )
        return "logged"
    if name == "get_measurements":
        result = vault.read_measurements(
            agent_id,
            tool_input.get("metric"),
            tool_input.get("limit"),
            tool_input.get("section") or vault.DEFAULT_TRACKING_SECTION,
        )
        entries = result["entries"]
        if not entries:
            return "(no measurements logged yet)"
        return "\n".join(
            f"{e['date']} — {e['metric']}: {e.get('value') or '(no value)'}"
            f"{' ' + e['unit'] if e.get('unit') else ''}"
            f"{' — ' + e['note'] if e.get('note') else ''}"
            for e in entries
        )
    if name == "add_foundation":
        vault.add_foundation(
            agent_id,
            tool_input.get("topic", "general"),
            tool_input.get("foundation", ""),
            tool_input.get("kind", ""),
            tool_input.get("reference", ""),
            tool_input.get("confidence"),
        )
        return "foundation added" + _size_note(vault.foundations_file(agent_id))
    if name == "get_foundations":
        result = vault.read_foundations(agent_id, tool_input.get("topic"))
        return result if result else "(no foundations recorded yet)"
    if name == "read_own_dashboard":
        html = vault.read_dashboard(agent_id)
        return html if html else "(no dashboard file yet)"
    if name == "check_page":
        import custom_pages
        return custom_pages.check(agent_id)
    if name in ("read_skill", "save_skill"):
        import skills
        try:
            if name == "read_skill":
                return skills.read(agent_id, tool_input.get("name", ""), tool_input.get("file"))
            s = skills.save_own(agent_id, tool_input.get("name", ""), tool_input.get("description", ""),
                                tool_input.get("instructions", ""))
            return f"skill saved: {s['name']} (in your folder, skills/{s['name']}/SKILL.md)"
        except skills.SkillError as exc:
            return f"refused: {exc}"
    if name == "write_page":
        import custom_pages
        try:
            return "page saved.\n" + custom_pages.write(agent_id, tool_input.get("page"), tool_input.get("html"))
        except custom_pages.PageError as exc:
            return f"refused: {exc}"
    if name == "restore_page":
        import custom_pages
        import page_history
        try:
            result = page_history.restore(agent_id, tool_input.get("kind") or "phone", tool_input.get("version"))
        except ValueError as exc:
            return f"refused: {exc}"
        return (f"v{result['restored']} is back ({', '.join(result['files'])}). Tell the user which version this is "
                f"and ask before changing the page again.\n" + custom_pages.check(agent_id))
    if name == "write_own_dashboard":
        html = tool_input.get("html", "")
        if len(html) > MAX_DASHBOARD_CHARS:
            return f"refused: {len(html)} chars exceeds the {MAX_DASHBOARD_CHARS}-char limit"
        import page_history
        page_history.safe_snapshot(agent_id)
        vault.write_dashboard(agent_id, html)
        return "dashboard saved — previous version backed up as dashboard.html.bak"
    if name == "message_agent":
        import agent_messages
        return agent_messages.tool_reply(agent_id, tool_input.get("agent", ""), tool_input.get("message", ""))
    if name == "flag_to_maintainer":
        item_id = vault.flag_to_maintainer(agent_id, tool_input.get("summary", ""))
        return f"flagged to the maintainer inbox (id: {item_id})"
    if name == "schedule_reminder":
        import reminders
        action = tool_input.get("action")
        try:
            if action == "add":
                r = reminders.add(agent_id, tool_input.get("message", ""), tool_input.get("at", ""), tool_input.get("repeat"),
                                  compose=bool(tool_input.get("compose")))
                when = datetime.strptime(r["next_at"], "%Y-%m-%dT%H:%M").strftime("%A %Y-%m-%d %H:%M")
                return (f"scheduled (id {r['id']}): {when}" + (f", then {r['repeat']}" if r["repeat"] else "")
                        + (" — you'll write the message then, from your instruction" if r["compose"] else ""))
            if action == "list":
                items = reminders.list_for(agent_id)
                if not items:
                    return f"nothing scheduled (now: {reminders.describe_now()})"
                lines = [f"- {r['id']}: {r['next_at'].replace('T', ' ')}" + (f" ({r['repeat']})" if r["repeat"] else "")
                         + (" [written when due]" if r["compose"] else "") + f" — {r['message'][:120]}" for r in items]
                return f"now: {reminders.describe_now()}\n" + "\n".join(lines)
            if action == "cancel":
                return "cancelled" if reminders.cancel(agent_id, tool_input.get("reminder_id", "")) else "no reminder with that id"
        except ValueError as exc:
            return f"not scheduled: {exc}"
        return f"unknown action: {action}"
    if name == "check_maintainer_request":
        item = vault.get_maintainer_inbox_item(tool_input.get("item_id", ""))
        if item is None:
            return "no inbox item with that id"
        result_note = f" — result: {item['result']}" if item.get("result") else ""
        return f"status: {item['status']}{result_note}"
    if name == "propose_to_owner":
        import proposals
        return proposals.tool_reply(agent_id, tool_input)
    if name == "update_maintainer_inbox_item":
        if not agents.has_role(agent_id, "maintainer"):
            return "refused: only the maintainer can update inbox items"
        try:
            updated = vault.update_maintainer_inbox_item(
                tool_input.get("item_id", ""), tool_input.get("status"), tool_input.get("result"),
            )
        except ValueError as exc:
            return f"refused: {exc}"
        return "updated" if updated else "no inbox item with that id"
    if name == "write_note":
        try:
            version = vault.write_note(
                agent_id, tool_input.get("path", ""), tool_input.get("content", ""),
                tool_input.get("expected_version"),
            )
        except (ValueError, vault.NoteConflictError) as exc:
            return f"refused: {exc}"
        return f"note saved (version {version})"
    if name == "read_note":
        try:
            result = vault.read_note(agent_id, tool_input.get("path", ""))
        except ValueError as exc:
            return f"refused: {exc}"
        if result is None:
            return "note not found"
        return f"(version {result['version']})\n{result['content']}"
    if name == "clave_backlog":
        import claves
        return claves.backlog_tool(agent_id, tool_input.get("action", ""), tool_input.get("item", ""),
                                   tool_input.get("clave", ""))
    if name == "write_vault_note":
        import vault_access
        return vault_access.write(agent_id, tool_input.get("path", ""), tool_input.get("content", ""),
                                  tool_input.get("mode", ""), tool_input.get("change_summary", ""))
    if name == "make_pdf":
        import pdf_export
        try:
            return pdf_export.make_pdf(tool_input.get("note", ""))
        except pdf_export.PdfError as exc:
            return f"no PDF: {exc}"
    if name == "list_notes":
        notes = vault.list_notes(agent_id)
        if not notes:
            return "(no notes yet)"
        return "\n".join(f"{n['path']} ({n['modified']})" for n in notes)
    return f"unknown tool: {name}"


def _summarize_transcript(transcript: str) -> str:
    import openai
    client = openai.OpenAI(api_key=os.environ["XAI_API_KEY"], base_url=XAI_BASE_URL)
    response = client.chat.completions.create(
        model=COMPACTION_MODEL,
        max_tokens=800,
        messages=[
            {
                "role": "system",
                "content": (
                    "Summarize this conversation transcript into a compact but complete record: concrete "
                    "facts stated, decisions made, values/measurements given, and any open threads. Write "
                    "it so a different assistant could pick up the conversation cold from this summary "
                    "alone. Be dense, not chatty — this replaces the raw turns for context purposes, so "
                    "don't lose anything concretely useful."
                ),
            },
            {"role": "user", "content": transcript},
        ],
    )
    return (response.choices[0].message.content or "").strip()


def maybe_compact_conversation(agent_id: str, conversation_id: str) -> None:
    """Called before building history for a chat call. No-ops for the vast
    majority of (short) conversations — only kicks in once raw size crosses
    COMPACTION_CHAR_THRESHOLD, and only if there are enough turns that
    compacting actually helps."""
    if vault.conversation_size(agent_id, conversation_id) < COMPACTION_CHAR_THRESHOLD:
        return
    turns = vault.raw_turn_blocks(agent_id, conversation_id)
    if len(turns) <= COMPACTION_KEEP_RECENT_TURNS:
        return
    to_summarize, keep = turns[:-COMPACTION_KEEP_RECENT_TURNS], turns[-COMPACTION_KEEP_RECENT_TURNS:]
    existing = vault.get_conversation(agent_id, conversation_id)
    prior_summary = (existing or {}).get("summary")
    transcript = ("Earlier summary:\n" + prior_summary + "\n\n" if prior_summary else "") + \
        "\n".join(t["raw"] for t in to_summarize)
    summary = _summarize_transcript(transcript)
    vault.compact_conversation(agent_id, conversation_id, summary, [t["raw"] for t in keep])


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


def _chat_xai(
    agent_id: str, model_id: str, system_prompt: str, messages: list[dict],
    api_model_id: str | None = None, reasoning_effort: str | None = None,
) -> str:
    # model_id is the catalog/selection id (used for usage logging, so
    # "Grok 4.6" vs "Grok 4.6 Fast" stay distinguishable there) — api_model_id
    # is the literal string xAI expects, which can differ (both grok-4.6
    # variants share one real model, distinguished only by reasoning_effort).
    import openai
    client = openai.OpenAI(api_key=os.environ["XAI_API_KEY"], base_url=XAI_BASE_URL)
    def openai_content(content):
        if not isinstance(content, list):
            return content
        return [{"type": "text", "text": b["text"]} if b["type"] == "text" else
                {"type": "image_url", "image_url": {"url": f"data:{b['media_type']};base64,{b['data']}"}}
                for b in content]
    convo = [{"role": "system", "content": system_prompt}] + [{**m, "content": openai_content(m["content"])} for m in messages]
    usage_total = {"input_tokens": 0, "output_tokens": 0}
    with _api_run(agent_id) as stop:
        for _ in range(MAX_TOOL_ROUNDS):
            _check_stop(stop)
            request_phase.set_phase(agent_id, "thinking")
            request_phase.reset_text(agent_id)
            stream = client.chat.completions.create(
                model=api_model_id or model_id,
                max_tokens=1024,
                tools=tools_for(agent_id),
                messages=convo,
                stream=True,
                stream_options={"include_usage": True},
                **({"reasoning_effort": reasoning_effort} if reasoning_effort else {}),
            )
            text, calls, finish_reason = _read_openai_stream(agent_id, stream, usage_total, stop)
            if finish_reason != "tool_calls" or not calls:
                log_usage(agent_id, "xai", model_id, usage_total, None)
                return text.strip()

            convo.append({"role": "assistant", "content": text or None, "tool_calls": calls})
            request_phase.set_phase(agent_id, "using tools")
            for call in calls:
                _check_stop(stop)
                args = json.loads(call["function"]["arguments"] or "{}")
                result = _run_tool(agent_id, call["function"]["name"], args)
                convo.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                })

    log_usage(agent_id, "xai", model_id, usage_total, None)
    return "(reached tool-use round limit without a final reply)"


def _read_openai_stream(agent_id: str, stream, usage_total: dict, stop: threading.Event) -> tuple[str, list[dict], str | None]:
    """Collects one streamed chat completion: the text (shown live through
    request_phase), the tool calls rebuilt from their pieces, and why it
    finished. Tool-call pieces arrive keyed by index: the first carries the
    id and name, later ones more of the arguments string."""
    text, calls, finish_reason = "", {}, None
    try:
        for chunk in stream:
            _check_stop(stop)
            if chunk.usage:
                usage_total["input_tokens"] += chunk.usage.prompt_tokens or 0
                usage_total["output_tokens"] += chunk.usage.completion_tokens or 0
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta and delta.content:
                text += delta.content
                request_phase.add_text(agent_id, delta.content)
            for piece in (delta.tool_calls or []) if delta else []:
                call = calls.setdefault(piece.index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if piece.id:
                    call["id"] = piece.id
                if piece.function and piece.function.name:
                    call["function"]["name"] += piece.function.name
                if piece.function and piece.function.arguments:
                    call["function"]["arguments"] += piece.function.arguments
            if choice.finish_reason:
                finish_reason = choice.finish_reason
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()
    return text, [calls[i] for i in sorted(calls)], finish_reason


def _anthropic_tools(agent_id: str) -> list[dict]:
    return [
        {
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "input_schema": t["function"]["parameters"],
        }
        for t in tools_for(agent_id)
    ]


def _anthropic_content(content):
    """Passes plain-string content through untouched (every history turn).
    The current turn, when it carries attachments, arrives as chat_service's
    neutral block shape ({"type":"image","media_type":...,"data":...}) —
    adapt just that into Anthropic's nested source-object shape."""
    if isinstance(content, str):
        return content
    return [
        {"type": "image", "source": {"type": "base64", "media_type": b["media_type"], "data": b["data"]}}
        if b.get("type") == "image" else b
        for b in content
    ]


def _chat_anthropic(agent_id: str, model_id: str, system_prompt: str, messages: list[dict], *, max_tokens: int | None = None, effort: str | None = None) -> str:
    import anthropic
    client = anthropic.Anthropic()
    convo = [{"role": m["role"], "content": _anthropic_content(m["content"])} for m in messages]
    # Prompt caching: mark the last tool definition and the system block as
    # cache breakpoints. Anthropic's cache prefix order is tools -> system ->
    # messages, and a breakpoint caches everything up to and including it —
    # so this one system-block marker is enough to cover both (tools and
    # system are both static per agent; conversation history isn't marked,
    # since it changes every turn and wouldn't hit anyway). Confirmed live
    # 2026-09-19 against Rocky's real ~30k-char prompt: first call wrote
    # ~16.5k cache tokens, every call after read them instead of paying full
    # input price again. The newer top-level "automatic" cache_control param
    # documented alongside this did NOT produce a cache hit in testing on
    # this SDK version (1.3.0) — use the explicit per-block form only.
    #
    # 1h TTL, not the 5m default: real conversations don't fire fast enough
    # for a 5-minute cache to matter. Confirmed live 2026-09-20 on Rocky's
    # actual usage — messages 14-20 minutes apart both landed as a full
    # cache_creation (paying the write price again) instead of a read,
    # because the default ephemeral cache had already expired between
    # turns. Writing at 1h TTL costs 2x input price instead of 1.25x, but
    # for this usage pattern (sparse messages, not rapid-fire) a hit some
    # time later at 0.1x is worth the pricier write.
    cache_control = {"type": "ephemeral", "ttl": "1h"}
    tools = _anthropic_tools(agent_id)
    if tools:
        tools[-1]["cache_control"] = cache_control
    system = [{"type": "text", "text": system_prompt, "cache_control": cache_control}]
    usage_total = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    }

    with _api_run(agent_id) as stop:
        return _anthropic_rounds(agent_id, client, model_id, system, tools, convo, usage_total, max_tokens, effort, stop)


def _anthropic_rounds(agent_id, client, model_id, system, tools, convo, usage_total, max_tokens, effort, stop) -> str:
    for _ in range(MAX_TOOL_ROUNDS):
        _check_stop(stop)
        request_phase.set_phase(agent_id, "thinking")
        request_phase.reset_text(agent_id)
        # Streamed so the pending bubble shows the reply as it's written
        # (request_phase text); the final message is the same as create's.
        with client.messages.stream(
            model=model_id,
            # Not just a generous reply budget: extended-thinking models (Opus
            # 5 confirmed live) spend real output tokens "thinking" before
            # any visible text, counted against this same cap. Reproduced
            # live at 1024: a large system prompt + a couple of tool calls
            # was enough to burn the whole budget on thinking mid-tool-call,
            # hit stop_reason="max_tokens" with zero text blocks, and return
            # a silently empty reply — no error, just nothing, every time
            # that combination came up. The voice path's override (see
            # chat()) is much lower (200) but safe from that failure mode:
            # voice mode's prompt has no tools/thinking to compete with the
            # budget, so the whole cap is available for visible text.
            max_tokens=max_tokens or 8192,
            system=system,
            tools=tools,
            messages=convo,
            **({"output_config": {"effort": effort}} if effort else {}),
        ) as stream:
            for event in stream:
                _check_stop(stop)
                if event.type == "text":
                    request_phase.add_text(agent_id, event.text)
            response = stream.get_final_message()
        u = response.usage
        usage_total["input_tokens"] += u.input_tokens or 0
        usage_total["output_tokens"] += u.output_tokens or 0
        usage_total["cache_creation_input_tokens"] += u.cache_creation_input_tokens or 0
        usage_total["cache_read_input_tokens"] += u.cache_read_input_tokens or 0

        if response.stop_reason != "tool_use":
            log_usage(agent_id, "anthropic", model_id, usage_total, None)
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            if not text:
                # Surfaced as a real error (run_chat_locked's ProviderReplyError
                # check) instead of silently persisting a blank turn — this is
                # exactly the failure mode the max_tokens bump above targets,
                # kept as a safety net in case some other combination still
                # exhausts the budget on thinking/tool-use with no visible reply.
                return f"(error: model stopped ({response.stop_reason}) without producing any reply text)"
            return text

        # The streamed message's text blocks carry a client-side parsed_output
        # field that the API rejects when sent back (400, seen 2026-10-13).
        convo.append({"role": "assistant", "content": [
            b.model_dump(exclude={"parsed_output"}) for b in response.content]})
        request_phase.set_phase(agent_id, "using tools")
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                _check_stop(stop)
                result = _run_tool(agent_id, block.name, block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        convo.append({"role": "user", "content": tool_results})

    log_usage(agent_id, "anthropic", model_id, usage_total, None)
    return "(reached tool-use round limit without a final reply)"


def _split_images(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Messages with every block-list content flattened to text (images become
    "[Attached image N]"), and the image blocks in order. Only the current
    turn ever carries images (chat_service)."""
    flat, images = [], []
    for message in messages:
        content = message["content"]
        if isinstance(content, list):
            parts = []
            for block in content:
                if block["type"] == "text":
                    parts.append(block["text"])
                elif block["type"] == "image":
                    images.append(block)
                    parts.append(f"[Attached image {len(images)}]")
            content = "\n".join(parts)
        flat.append({**message, "content": content})
    return flat, images


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


def claude_phase_for(event: dict) -> str | None:
    if event.get("type") != "assistant":
        return None
    content = (event.get("message") or {}).get("content") or []
    for block in content:
        if block.get("type") == "tool_use":
            return tool_phase(str(block.get("name") or ""), block.get("input"))
    return "thinking" if content else None


def _watch_claude_phase(agent_id: str, output_path: Path, stop: threading.Event) -> None:
    offset = 0
    while not stop.wait(0.4):
        try:
            with output_path.open("rb") as f:
                f.seek(offset)
                chunk = f.read()
            end = chunk.rfind(b"\n") + 1
            offset += end
            for line in chunk[:end].decode("utf-8", "replace").splitlines():
                try:
                    phase = claude_phase_for(json.loads(line))
                    if phase:
                        request_phase.set_phase(agent_id, phase)
                except (ValueError, TypeError, AttributeError):
                    continue
        except OSError:
            pass  # progress is cosmetic


def claude_steps_done(output: str, limit: int = 40) -> list[str]:
    """One line per tool call that got a result in a (partial) Claude CLI
    stream, oldest first, so a stopped run can say what it already did."""
    calls, done = {}, []
    for line in (output or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        content = (event.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                calls[block.get("id")] = block
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                call = calls.pop(block["tool_use_id"])
                name = str(call.get("name") or "tool").split("__")[-1]
                args = json.dumps(call.get("input") or {}, ensure_ascii=False)
                failed = " (failed)" if block.get("is_error") else ""
                done.append(f"{name} {args[:160]}{'…' if len(args) > 160 else ''}{failed}")
    if len(done) > limit:
        done = [f"… {len(done) - limit} earlier steps"] + done[-limit:]
    return done


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


def _chat_claude_cli(agent_id: str, model_id: str, system_prompt: str, messages: list[dict],
                     timeout_s: int | None = None) -> str:
    """Routes through the Claude Pro/Max subscription via `claude -p` instead of a
    paid API key. Every built-in tool (Bash, file Read/Write/Edit, WebFetch, ...)
    is explicitly disabled (`--tools ""`) — the only capability the model has is
    the vault MCP server (mcp_vault_server.py), spawned scoped to this one
    agent_id, exposing exactly the same tools/behavior as the xAI/Anthropic-API
    paths via the shared _run_tool dispatcher. No session persistence: this call
    is stateless like the other two paths, with the full conversation formatted
    into one prompt rather than resumed across calls.
    """
    mcp_config = {
        "mcpServers": {
            "vault": {
                "command": sys.executable,
                "args": [MCP_VAULT_SERVER],
                "env": {"OPENCLAVE_AGENT_ID": agent_id},
            },
            **_extra_servers(agent_id),
        }
    }

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(mcp_config, f)
        mcp_config_path = f.name

    # openclave-service's own EnvironmentFile sets ANTHROPIC_API_KEY (the separate,
    # $0-balance Console key used by _chat_anthropic) — Claude CLI treats any
    # present API key as taking precedence over the OAuth/subscription login, so
    # it must be stripped here or every claude-cli call silently falls back to
    # the dead API key instead of the subscription.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    run_marker = _mark_run(agent_id, env)

    stop_watch = threading.Event()
    timeout_s = timeout_s or _cli_timeout(model_id, CLAUDE_CLI_TIMEOUT_S)
    with tempfile.NamedTemporaryFile("w+b") as output_file:
        watcher = threading.Thread(target=_watch_claude_phase, args=(agent_id, Path(output_file.name), stop_watch), daemon=True)
        watcher.start()
        try:
            # Images go in through stream-json input (verified 2026-10-04);
            # text-only turns keep the plain -p prompt.
            flat, images = _split_images(messages)
            if images:
                prompt_args = ["-p", "--input-format", "stream-json"]
                stdin_line = json.dumps({"type": "user", "message": {"role": "user", "content": [
                    {"type": "text", "text": _format_transcript(flat)},
                    *({"type": "image", "source": {"type": "base64", "media_type": b["media_type"], "data": b["data"]}} for b in images),
                ]}}) + "\n"
            else:
                prompt_args, stdin_line = ["-p", _format_transcript(flat)], None
            result = subprocess.run(
                [
                    CLAUDE_BIN, *prompt_args,
                    "--model", model_id,
                    "--system-prompt", system_prompt,
                    "--tools", _cli_tools(agent_id) if _has_web_search(agent_id) else "",
                    "--mcp-config", mcp_config_path,
                    "--strict-mcp-config",
                    "--permission-mode", "bypassPermissions",
                    "--output-format", "stream-json",
                    "--verbose",
                    "--no-session-persistence",
                    "--setting-sources", "",
                ],
                **({"input": stdin_line} if stdin_line else {"stdin": subprocess.DEVNULL}),
                stdout=output_file,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_s,
                cwd=tempfile.gettempdir(),
                env=env,
                start_new_session=True,
            )
            output_file.flush()
            output_file.seek(0)
            output = output_file.read().decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            cancel_running(agent_id)  # its MCP servers live on in the process group
            output_file.flush()
            output_file.seek(0)
            partial = output_file.read().decode("utf-8", "replace")
            return _stopped_reply("claude-cli", model_id, timeout_s, claude_steps_done(partial))
        finally:
            _unmark_run(agent_id, run_marker)
            stop_watch.set()
            watcher.join(timeout=2)
            os.unlink(mcp_config_path)

    if result.returncode != 0:
        # Claude CLI puts the readable reason in its output stream (the final
        # result event, or the assistant's error message); stderr often has
        # just a code or nothing, which left "claude-cli error: " (2026-10).
        reason = _claude_stream_error(output)
        if claude_auth.is_auth_failure(output, reason or result.stderr):
            claude_auth.mark_signed_out(reason)
            return claude_auth.AGENT_REPLY
        if reason:
            return f"(claude-cli error: exit code {result.returncode}: {reason})"
        return f"(claude-cli error: {_cli_diagnostic(result.stderr, result.returncode)})"

    try:
        payload = next(event for event in reversed([json.loads(line) for line in output.splitlines()]) if event.get("type") == "result")
    except (json.JSONDecodeError, StopIteration):
        return "(claude-cli error: no final result in output stream)"

    log_usage(agent_id, "claude-cli", model_id, payload.get("usage") or {}, payload.get("total_cost_usd"))

    if payload.get("is_error"):
        if claude_auth.is_auth_failure(output, str(payload.get("result") or "")):
            claude_auth.mark_signed_out(str(payload.get("result") or ""))
            return claude_auth.AGENT_REPLY
        return f"(claude-cli error: {payload.get('result', 'unknown error')})"

    claude_auth.mark_ok()
    return (payload.get("result") or "").strip()


def _toml_str(s: str) -> str:
    # TOML basic strings share JSON's escaping for the plain ASCII paths/ids/
    # values we ever pass here, so json.dumps is a safe, dependency-free way
    # to quote a value for a `-c key=value` override.
    return json.dumps(s)


# Codex's MCP write tools (update_backlog, update_mission, etc.) are always
# blocked headlessly — see _chat_codex_cli's docstring. This is the fallback
# path taught to the model instead: edit the file directly, scoped by the
# workspace-write sandbox to its own agent folder. Deliberately excludes
# tracking.md and dashboard.html — both have formats (multi-section tables;
# templated HTML/JS) other code depends on parsing correctly, too easy to
# corrupt with a freeform edit for the value it'd add.
CODEX_WRITE_GUIDANCE = """
# Codex-specific capability notes

Your MCP tools for writing (update_backlog, update_plan_phase, update_mission,
update_persona, add_feedback, write_memory, log_measurement, add_foundation,
write_own_dashboard, write_note, flag_to_maintainer, update_maintainer_inbox_item) will
NOT work under this provider — every call to them fails with an approval
error. Do not retry them.

flag_to_maintainer and update_maintainer_inbox_item have no native-file-tool workaround
like the ones below — they write to the maintainer inbox, outside your folder.
If something genuinely needs maintainer attention, tell the user directly.
check_maintainer_request (MCP,
read-only) still works if you already have an item id from a prior session.

Instead, for backlog/mission/persona changes, edit the file directly with
your own file tools. You can read anywhere, but you can only write inside
your own agent folder (your current working directory) — the same boundary
the MCP tools would have enforced, just applied by the sandbox instead.
Always read the file first so you preserve its exact existing structure,
and make the smallest edit that accomplishes the change rather than
rewriting the whole file. Mission has a 1,850-character limit and Persona a
2,200-character limit, including frontmatter. If an edit cannot fit, do not
silently drop the improvement: explain that it was not stored. The service
will restore an over-limit direct edit, send its proposed content to the maintainer,
and append a notice to the user's reply.

- backlog.md: YAML frontmatter, then a '# Backlog' section of
  '- [ ] item text' / '- [x] item text' lines. To add an item, append a new
  '- [ ] ...' line. To check/uncheck, flip '[ ]'/'[x]' on the matching line.
- mission.md / persona.md: YAML frontmatter, then a freeform markdown body.
  Replace the whole body when it changes, but keep sections that are still
  true — don't just append. mission.md's '## Domains' list is read by other
  agents; only change it deliberately.

- Freeform notes (a per-project file, an inventory list, working notes not
  covered by anything above): create/edit them directly with your own file
  tools too, anywhere in your own folder, as long as the filename ends in
  .md or .txt. read_note/list_notes (MCP, read-only) still work if useful,
  but you don't need them just to write — your native file tools already
  have the access.

- Your own skills: save_skill won't work here either. Write
  skills/<name>/SKILL.md in your folder: front matter with name and a
  one-sentence description, then the steps. read_skill (MCP, read-only)
  still works.

- Your phone page: write page.json (and page.html for an html block) in
  your own folder with your own file tools — write_page won't work here.
  Call check_page (MCP, read-only) first for the format and again after
  writing, and fix anything it reports.

Do NOT directly edit tracking.md or dashboard.html — their formats (multi-
section tables; templated HTML/JS) are easy to corrupt with a freeform edit,
and other tools depend on them parsing correctly. If a tracking entry or
dashboard change seems called for, say so to the user rather than attempting it
yourself.

Reading is unaffected by any of this — search_vault, read_vault_file, and
reading files directly all still work everywhere in the vault.
""".strip()


def _chat_codex_cli(agent_id: str, model_id: str, system_prompt: str, messages: list[dict]) -> str:
    """Routes through the ChatGPT Plus/Pro subscription via `codex exec` instead of
    a paid API key, mirroring _chat_claude_cli's reasoning, but two things about
    Codex's design meant the same shape didn't carry over — both found by live
    testing, not from docs:

    - MCP tool calls need per-tool `read_only_hint` annotations to run at all
      headlessly (`codex exec` hardcodes its approval policy to "never", and
      "never" means *deny anything that would need approval*, not *auto-allow*
      — confirmed live). Only mcp_vault_server.py's read tools carry that
      annotation, so this path is read-only through MCP; writes go through
      CODEX_WRITE_GUIDANCE's fallback instead — see there for why.
    - No `--mcp-config` flag: MCP configuration normally comes from the global
      Codex home. Sharing that config across agents caused two real isolation
      failures: Codex once selected Stash's vault server for Reba, and a
      malformed Spice entry later prevented Reba from starting at all. Each
      call now gets a temporary CODEX_HOME with its own vault server and, for
      R2, its Home Assistant admin server. The
      user's auth file is linked into it, so subscription authentication is
      shared while configuration and tools are isolated per agent.

    The actual write path is `-s workspace-write` scoped to the agent's own
    vault folder via `-C` — verified live (2026-09-15, after fixing an Ubuntu
    24.04 AppArmor default that silently broke Codex's bundled bubblewrap
    sandbox — see Build Docs/Archive/OpenClave — Open Sourcing.md) that a write
    inside that folder succeeds with no approval prompt, and a write outside
    it is flatly rejected: "patch rejected: writing outside of the project".
    `/tmp` is always also writable regardless of this scoping — harmless,
    nothing sensitive lives there, just a known Codex sandbox default.

    No `--system-prompt` flag either — `-c model_instructions_file=<path>`
    (found by inspecting the codex binary's own config keys) is the closest
    equivalent, not a confirmed-identical mechanism, so the system prompt is
    *also* prepended directly into the prompt text as a belt-and-suspenders
    fallback.
    """
    full_system_prompt = f"{system_prompt}\n\n{CODEX_WRITE_GUIDANCE}"

    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(full_system_prompt)
        instructions_path = f.name

    last_message_fd, last_message_path = tempfile.mkstemp(suffix=".txt")
    os.close(last_message_fd)

    # openclave-service's own EnvironmentFile sets OPENAI_API_KEY (unused by this
    # path) — stripped for the same reason _chat_claude_cli strips
    # ANTHROPIC_API_KEY: a present API key can silently take precedence over
    # the OAuth/subscription login.
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    # The system service inherits OpenClaw-specific XDG paths. Codex does not
    # need them, and allowing them through makes its configuration depend on
    # an unrelated service's environment.
    env.pop("XDG_CONFIG_HOME", None)
    env.pop("XDG_CACHE_HOME", None)

    source_codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    server_name = f"openclave-vault-{agent_id}"

    try:
        with tempfile.TemporaryDirectory(prefix=f"openclave-codex-{agent_id}-") as codex_home:
            codex_home_path = Path(codex_home)
            # Pass real image files to Codex; never stringify image blocks
            # (which would put base64 in the transcript instead of pixels).
            image_args = []
            text_messages = []
            for message in messages:
                content = message["content"]
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if block["type"] == "text":
                            parts.append(block["text"])
                        elif block["type"] == "image":
                            suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}[block["media_type"]]
                            image_path = codex_home_path / f"attachment-{len(image_args) // 2 + 1}{suffix}"
                            image_path.write_bytes(base64.b64decode(block["data"], validate=True))
                            image_args.extend(["--image", str(image_path)])
                            parts.append(f"[Attached image {len(image_args) // 2}]")
                        else:
                            raise ValueError(f"Unsupported Codex content block: {block['type']}")
                    content = "\n".join(parts)
                text_messages.append({**message, "content": content})
            prompt = f"<system_instructions>\n{full_system_prompt}\n</system_instructions>\n\n" + _format_transcript(text_messages)
            config_lines = [
                        'model_reasoning_effort = "low"',
                        # Live, not Codex's default cached index: agents look up
                        # current specs, prices and events (2026-09-25).
                        f'web_search = "{"live" if _has_web_search(agent_id) else "disabled"}"',

                        "",
                        f'[mcp_servers.{_toml_str(server_name)}]',
                        f"command = {_toml_str(sys.executable)}",
                        f"args = [{_toml_str(MCP_VAULT_SERVER)}]",
                        "",
                        f'[mcp_servers.{_toml_str(server_name)}.env]',
                        f"OPENCLAVE_AGENT_ID = {_toml_str(agent_id)}",
                        "",
            ]
            cfg = agents.AGENTS.get(agent_id)
            for key, definition in (cfg.mcp_servers.items() if cfg else ()):
                server = _expand_server_env(definition)
                codex = server.get("codex")
                if not isinstance(codex, dict):
                    continue
                codex_name = codex.get("name", key)
                config_lines.extend([
                    f'[mcp_servers.{_toml_str(codex_name)}]',
                    f'command = {_toml_str(server["command"])}',
                    f'args = [{", ".join(_toml_str(arg) for arg in server.get("args", []))}]',
                    f'env_vars = [{", ".join(_toml_str(k) for k in codex.get("env_vars", server.get("env", {}).keys()))}]',
                    f'default_tools_approval_mode = {_toml_str(codex.get("default_tools_approval_mode", "auto"))}',
                    f'required = {"true" if codex.get("required", True) else "false"}',
                    '',
                ])
                # Values the agent data sets itself (not forwarded from the
                # service env by name) go in the server's env table.
                forwarded = set(codex.get("env_vars", server.get("env", {}).keys()))
                run_env = {k: str(v) for k, v in server.get("env", {}).items() if k not in forwarded}
                run_env.update(RUN_ENV.get(agent_id, {}))
                if agent_id in READ_ONLY_RUNS and codex.get("read_only_env"):
                    run_env[codex["read_only_env"]] = "1"
                if run_env:
                    config_lines.append(f'[mcp_servers.{_toml_str(codex_name)}.env]')
                    config_lines.extend(f'{_toml_str(k)} = {_toml_str(v)}' for k, v in run_env.items())
                    config_lines.append('')
            (codex_home_path / "config.toml").write_text("\n".join(config_lines))

            # Auth is the one intentionally shared piece of Codex state. A
            # symlink avoids copying a bearer token and leaves no credential
            # behind when the temporary home is removed.
            source_auth = source_codex_home / "auth.json"
            if source_auth.is_file():
                (codex_home_path / "auth.json").symlink_to(source_auth)

            env["CODEX_HOME"] = codex_home
            run_marker = _mark_run(agent_id, env)
            stop_watch = threading.Event()
            watcher = threading.Thread(target=_watch_codex_phase, args=(agent_id, codex_home_path, stop_watch), daemon=True)
            watcher.start()
            timeout_s = _cli_timeout(model_id, CODEX_CLI_TIMEOUT_S)
            try:
                result = subprocess.run(
                    [
                        CODEX_BIN, "exec",
                        *(["--approve-for-me"] if agents.is_home_assistant_agent(agent_id) else []),
                        *([] if agents.is_home_assistant_agent(agent_id) else ["-s", "workspace-write"]),
                        "-C", str(vault.agent_root(agent_id)),
                        "--skip-git-repo-check",
                        "-c", f"model_instructions_file={_toml_str(instructions_path)}",
                        "-m", model_id,
                        "--output-last-message", last_message_path,
                        *image_args,
                        "--",
                        prompt,
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                    env=env,
                    start_new_session=True,
                )
            except subprocess.TimeoutExpired:
                cancel_running(agent_id)  # Codex's sandbox and MCP children too
                return _stopped_reply("codex-cli", model_id, timeout_s, codex_steps_done(codex_home_path))
            finally:
                _unmark_run(agent_id, run_marker)
                stop_watch.set()
                watcher.join(timeout=2)
            _record_codex_rate_limits(codex_home_path)
            _log_codex_usage(agent_id, model_id, codex_home_path)
    finally:
        os.unlink(instructions_path)
        # Also clean up when launching Codex fails or times out.
        if 'result' not in locals():
            Path(last_message_path).unlink(missing_ok=True)

    try:
        if result.returncode != 0:
            return f"(codex-cli error: {_cli_diagnostic(result.stderr, result.returncode)})"
        last_message_file = Path(last_message_path)
        reply = last_message_file.read_text().strip() if last_message_file.exists() else ""
        return reply or "(codex-cli returned no final message)"
    finally:
        Path(last_message_path).unlink(missing_ok=True)
