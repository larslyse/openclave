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


# Same reasoning, same fix, for the OpenAI Codex CLI (subscription path via
# `codex exec` — see _chat_codex_cli).
CODEX_BIN = config.CONFIG.codex_bin
CODEX_CLI_TIMEOUT_S = 300


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
    # The service's environment may carry XDG paths meant for other programs.
    # Codex does not need them, and allowing them through makes its
    # configuration depend on an unrelated service's environment.
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
