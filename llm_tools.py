"""Running the agent tools for the API routes (and the vault MCP server), split
out of llm_client.py: each agent's tool list, the dispatcher, the per-call
tool log and the activity label a tool call shows."""
from datetime import datetime
from pathlib import Path

import agents
import request_phase
import vault
from llm_tool_defs import TOOLS


MAX_TOOL_ROUNDS = 5
MAX_DASHBOARD_CHARS = 100_000


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
