"""The claude-cli route: `claude -p` on the user's Claude subscription, one
subprocess per reply, with the agent's tools served by the vault MCP server
(split out of llm_client.py)."""
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import claude_auth
import config
import request_phase
from llm_runs import (
    MCP_VAULT_SERVER, _cli_diagnostic, _cli_timeout, _cli_tools, _extra_servers,
    _format_transcript, _mark_run, _stopped_reply, _unmark_run, cancel_running,
)
from llm_tools import _has_web_search, tool_phase
from llm_usage import log_usage
from providers.base import Provider


CLAUDE_CLI_TIMEOUT_S = 300


# Absolute path, not "claude": systemd services don't inherit an interactive
# shell's PATH, so a bare "claude" 404s under uvicorn even though it resolves fine
# from a login shell. This is the stable ~/.local/bin symlink, not the versioned
# target, so it keeps resolving correctly across `claude update`.
CLAUDE_BIN = config.CONFIG.claude_bin


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


class ClaudeCliProvider(Provider):
    id = "claude-cli"
    route = "CLI"

    def chat(self, agent_id, model, system_prompt, messages, *, max_tokens=None, effort=None):
        # The timeout follows the catalog entry (its speed), not the API id it
        # runs under: claude-opus-5-5-cli is slow, claude-opus-5-5 is not, which
        # cut R2's big HA builds off at 5 minutes instead of 10 (2026-10-17).
        return _chat_claude_cli(agent_id, model.api_model_id or model.id, system_prompt, messages,
                                timeout_s=_cli_timeout(model.id, CLAUDE_CLI_TIMEOUT_S))
