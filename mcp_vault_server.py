"""Stdio MCP server exposing OpenClave's vault tools to claude-cli and
codex-cli calls.

Spawned fresh per chat call (see llm_client._chat_claude_cli / _chat_codex_cli),
scoped to a single agent via the OPENCLAVE_AGENT_ID env var — never a
long-lived shared server, so cross-agent write bleed stays structurally
impossible (Build Requirements §3). Every tool delegates to
llm_client._run_tool, the same dispatcher the xAI/Anthropic-API chat paths
use, so tool behavior can't diverge across providers.
"""
import os
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

import llm_client

_READ_ONLY = ToolAnnotations(read_only_hint=True)

AGENT_ID = os.environ["OPENCLAVE_AGENT_ID"]

mcp = MCPServer("openclave-vault")

import agent_tools  # noqa: E402

_ALLOWED = agent_tools.allowed(AGENT_ID)


def _tool(**kwargs):
    """mcp.tool() for the tools this agent has (agent_tools.py); others are
    never registered, so the model doesn't see them at all."""
    def register(fn):
        return mcp.tool(**kwargs)(fn) if fn.__name__ in _ALLOWED else fn
    return register


def _run(name: str, **kwargs) -> str:
    return llm_client._run_tool(AGENT_ID, name, kwargs)


@_tool()
def update_backlog(action: Literal["add", "check", "uncheck"], item: str) -> str:
    """Add a new open item to the agent's backlog, or mark an existing item done/not done.

    item is the backlog item text (for 'add'), or a substring identifying an
    existing item (for 'check'/'uncheck').
    """
    return _run("update_backlog", action=action, item=item)


@_tool()
def update_plan_phase(
    phase: str, percent: int, status: Literal["not-started", "in-progress", "complete"] | None = None
) -> str:
    """Update the completion percent and/or status of a phase in the current plan."""
    return _run("update_plan_phase", phase=phase, percent=percent, status=status)


@_tool()
def update_mission(body: str) -> str:
    """Replace your 'mission.md' body — the living baseline of who the user is and what
    you're tracking for them. Use this whenever conversation reveals something that
    should update your standing context (a changed constraint, a new goal, a
    corrected fact) rather than just living in this session's chat log. Write the
    full new body — keep sections that are still true, don't just append. This file
    also has a '## Domains' bullet list other agents see as your expertise tags —
    keep it intact (or update deliberately) when you rewrite the body.
    """
    return _run("update_mission", body=body)


@_tool()
def update_persona(body: str) -> str:
    """Replace your own persona.md body — who you are and your tone. Use this
    rarely: only when the user explicitly asks you to change how you operate, or
    you've found a genuinely better way to describe your own lane. Mention in
    your reply that you changed it so the user notices.
    """
    return _run("update_persona", body=body)


@_tool()
def add_feedback(entry: str) -> str:
    """Log one specific correction or confirmation about HOW you interact with
    the user, appended to your own persona.md under '## Feedback'. Not a domain
    fact (update_mission) or a full personality rewrite (update_persona) — a
    short durable interaction rule. Use for both corrections and quiet
    confirmations, and log it right when it happens.
    """
    return _run("add_feedback", entry=entry)


@_tool(annotations=_READ_ONLY, description=next(t["function"]["description"] for t in llm_client.TOOLS
                                                if t["function"]["name"] == "search_vault"))
def search_vault(query: str) -> str:
    """Keyword-search the owner's whole vault (description shared with llm_client.TOOLS)."""
    return _run("search_vault", query=query)


@_tool(annotations=_READ_ONLY)
def read_vault_file(path: str) -> str:
    """Read the full contents of one specific vault file using an absolute
    vault path from your context directory or a qmd:// URI from search_vault.
    """
    return _run("read_vault_file", path=path)


@_tool()
def write_memory(title: str, body: str) -> str:
    """Save a freeform entry in your own memory/ folder — the LOWEST-priority
    bucket, for genuinely one-off things. Not auto-loaded into future chats —
    use list_memories / read_memory to pull one back up when relevant. Do NOT
    use this for a durable protocol/equipment fact (that's update_mission) or
    dated information (that's log_measurement, in a section from tracking.md).
    """
    return _run("write_memory", title=title, body=body)


@_tool(annotations=_READ_ONLY)
def list_memories() -> str:
    """List your own saved memory entries (title + date), most recent first."""
    return _run("list_memories")


@_tool(annotations=_READ_ONLY)
def read_memory(entry_id: str) -> str:
    """Read the full content of one of your own memory entries, by id (from list_memories)."""
    return _run("read_memory", entry_id=entry_id)


@_tool()
def log_measurement(
    metric: str,
    value: str | None = None,
    unit: str | None = None,
    note: str | None = None,
    date: str | None = None,
    section: str = "Progress",
) -> str:
    """Record one dated measurement, activity, or qualitative note in your
    tracking log. This is the home for ANYTHING dated worth watching over
    time, number or free text: if forgetting it by next session would change
    the advice you give, and it's tied to a date rather than a standing fact,
    it belongs here, not in memory/. Call get_measurements with no metric
    filter first if unsure what metric names are already in use, so you reuse
    the same name consistently instead of a near-duplicate.

    section is a heading from your tracking.md. Reuse an existing heading;
    if there are no headings yet, the default is Progress.
    """
    return _run("log_measurement", metric=metric, value=value, unit=unit, note=note, date=date, section=section)


@_tool(annotations=_READ_ONLY)
def get_measurements(metric: str | None = None, limit: int | None = None, section: str = "Progress") -> str:
    """Read back tracking entries from one section, most recent first. Omit
    metric to see everything logged in that section, or pass metric to filter
    to one trend. section is a heading from your tracking.md and defaults to
    Progress.
    """
    return _run("get_measurements", metric=metric, limit=limit, section=section)


@_tool()
def add_foundation(topic: str, foundation: str, kind: Literal["claim", "operating_rule"],
                   reference: str, confidence: int | None = None) -> str:
    """Record a durable decision basis in optional foundations.md, only when useful.
    Claims need a source and uncertainty; confidence (1-10) is optional.
    Agreed operating rules need rationale, with no confidence score.
    Read get_foundations first; do not invent entries to fill the file.
    """
    return _run("add_foundation", topic=topic, foundation=foundation, kind=kind,
                reference=reference, confidence=confidence)


@_tool(annotations=_READ_ONLY)
def get_foundations(topic: str | None = None) -> str:
    """Read back recorded foundations — one topic if given, or every topic
    concatenated if omitted. Use before add_foundation to avoid duplicates.
    """
    return _run("get_foundations", topic=topic)


@_tool(annotations=_READ_ONLY)
def read_skill(name: str, file: str | None = None) -> str:
    """Load one of your skills (listed under Skills in your instructions)
    before doing a job it covers, then follow it. file= reads another file
    that comes with the skill.
    """
    return _run("read_skill", name=name, file=file)


@_tool()
def save_skill(name: str, description: str, instructions: str) -> str:
    """Write down how to do a recurring job as one of your own skills, so
    next time you (or a scheduled task) can follow it. name: short lowercase,
    e.g. 'ha-update'. description: one sentence on what it's for and when to
    use it. instructions: the steps and rules, in Markdown. An existing name
    of yours is rewritten; read it first and keep what still holds.
    """
    return _run("save_skill", name=name, description=description, instructions=instructions)


@_tool(annotations=_READ_ONLY)
def check_page() -> str:
    """Your own page in the user's phone app (the button left of your name):
    shows the page format and what the user currently sees, with any errors. Call
    it before building or changing your page, and again after writing
    page.json.
    """
    return _run("check_page")


@_tool()
def write_page(page: dict, html: str | None = None) -> str:
    """Save your own page for the user's phone app (page.json in your folder;
    call check_page first for the format). Checked before saving and refused
    with the reason if a block is wrong. html: optional full HTML saved as
    page.html, for an html block.
    """
    return _run("write_page", page=page, html=html)


# Marked read-only only so Codex's approval gate lets it through (same as
# finish_onboarding): it writes nothing but this agent's own page files, back
# to a version that was already there (page_history.py).
@_tool(annotations=_READ_ONLY)
def restore_page(kind: Literal["phone", "desktop"] = "phone", version: int | None = None) -> str:
    """Put an earlier version of your page back, e.g. when the user says "revert
    your page". kind: "phone" (page.json, the default) or "desktop"
    (dashboard.html). version: from check_page's version list; leave it out
    for the last phone version the user kept (desktop: the one before now).
    Afterwards tell the user which version is back and ask before changing it again.
    """
    return _run("restore_page", kind=kind, version=version)


@_tool(annotations=_READ_ONLY)
def find_voice(query: str, language: str = "en") -> str:
    """Search Fish Audio's voice library for a voice for you to speak with in
    voice mode. Returns a few voices with id, description and a sample link.
    Give the user 2-3 that fit, each with its sample link so they can listen, and
    let them pick; then set_voice. query e.g. 'calm deep male', 'warm british female'.
    """
    return _run("find_voice", query=query, language=language)


# Marked read-only only so Codex's approval gate lets it through (same as
# finish_onboarding): it writes nothing but the voice in this agent's agent.json.
@_tool(annotations=_READ_ONLY)
def set_voice(voice_id: str) -> str:
    """Use this Fish Audio voice (id from find_voice) for your voice-mode
    replies from now on. Only the one the user picked.
    """
    return _run("set_voice", voice_id=voice_id)


@_tool(annotations=_READ_ONLY)
def read_own_dashboard() -> str:
    """Read the current HTML of your own dashboard page, so you can edit it
    incrementally instead of guessing its current state.
    """
    return _run("read_own_dashboard")


@_tool()
def write_own_dashboard(html: str) -> str:
    """Replace your own dashboard page with new HTML. Inline your own <style> and
    <script>, no build step or restart needed. The previous version is
    automatically backed up before being overwritten. Your page runs sandboxed
    (no access to other agents' pages or endpoints) — plain fetch('/<agent>/...')
    calls to your OWN endpoints keep working unchanged. localStorage/
    sessionStorage work but don't persist across a reload.
    Shared chat/conversation-list/backlog/kanban/usage/markdown-rendering
    behavior lives in /static/dashboard_common.css and dashboard_common.js —
    link/script-src those rather than pasting your own copies. read_own_dashboard
    shows the current pattern (color variables + skeleton + those two includes
    + your own custom panels only).
    """
    return _run("write_own_dashboard", html=html)


@_tool()
def write_note(path: str, content: str, expected_version: str | None = None) -> str:
    """Create or update a plain note file inside your own folder — for structured,
    ongoing content that doesn't fit your other tools (a per-project file, an
    inventory list, working notes). Path is relative to your own folder root
    and must end in .md or .txt. Reserved paths (dashboard.html, backlog.md,
    plan.md, mission.md, persona.md, foundations.md, tracking.md,
    conversations/, memory/) are refused. To update a note that already
    exists, call read_note first and pass back its version.
    """
    return _run("write_note", path=path, content=content, expected_version=expected_version)


@_tool(annotations=_READ_ONLY)
def read_note(path: str) -> str:
    """Read one of your own note files by path (from list_notes or a path you
    already know). Returns a version marker to pass back to write_note when
    updating it.
    """
    return _run("read_note", path=path)


@_tool(annotations=_READ_ONLY)
def list_notes() -> str:
    """List your own note files created via write_note, most recently modified first."""
    return _run("list_notes")


# Read-only flag for Codex's gate, as below; it writes only the clave's backlog.md.
@_tool(annotations=_READ_ONLY)
def clave_backlog(action: Literal["read", "add", "check", "uncheck"], item: str = "", clave: str = "") -> str:
    """Your clave's shared backlog (work that spans its agents). read it, add
    an item, or (the lead only, when there is one) check/uncheck an item by
    part of its text. clave: its id, only needed if you are in more than one."""
    return _run("clave_backlog", action=action, item=item, clave=clave)


# Flagged read-only only so Codex's approval gate lets it through (Codex
# blocks every MCP tool without read_only_hint); the folder scope is the
# real limit (vault_access.py).
@_tool(annotations=_READ_ONLY)
def write_vault_note(path: str, content: str, mode: Literal["append", "create"], change_summary: str) -> str:
    """Write in the vault folders the owner gave you beyond your own folder
    (listed in your instructions; refused anywhere else). mode 'append' adds to
    an existing note, 'create' makes a new note in an existing folder. Never
    deletes. Search first and prefer appending."""
    return _run("write_vault_note", path=path, content=content, mode=mode, change_summary=change_summary)


# Marked read-only only so Codex's headless approval gate lets it through
# (Codex blocks every MCP tool without read_only_hint). It never changes a
# note: it only writes the note's .pdf next to it, and never in configured
# confidential folders.
@_tool(annotations=_READ_ONLY)
def make_pdf(note: str) -> str:
    """Make a print-ready A4 PDF of a note anywhere in the user's vault, saved next
    to the note with the same name (.pdf), so it syncs wherever the
    user's notes sync. Use it when the user asks for a PDF. Figures stay with their captions,
    tables and short sections are kept on one page. To force a new page, put
    <div class="page-break"></div> on its own line in the note (edit the note
    first if the user wants that); an italic line right after an image becomes its
    caption. note: its vault path, a path or qmd:// URI from search_vault, or
    just its name. Notes in configured confidential folders are never exported.
    """
    return _run("make_pdf", note=note)


@_tool()
def message_agent(agent: str, message: str) -> str:
    """Send another OpenClave agent a message: a question in their domain, or
    data they should keep (a purchase to log, a measurement, a finding). It
    lands in their 'From <you>' conversation, they act on it and answer there,
    and the user can read it. You don't get the answer back unless they message
    you. Put everything they need in the message itself. agent: the
    recipient's name from the agent roster.
    """
    return _run("message_agent", agent=agent, message=message)


@_tool()
def flag_to_maintainer(summary: str) -> str:
    """Send an improvement for the OpenClave app itself to the maintainer:
    a bug, a broken or missing tool, or a feature idea. Not for research,
    lookups or domain questions: do those yourself (you have web search).
    Returns an item id; use check_maintainer_request with it later if the outcome
    matters.
    """
    return _run("flag_to_maintainer", summary=summary)


@_tool(annotations=_READ_ONLY)
def check_maintainer_request(item_id: str) -> str:
    """Check the status/result of something previously flagged to the maintainer via
    flag_to_maintainer, using the id it returned. Status is 'open' (not yet looked
    at), 'acknowledged' (seen, no result yet), or 'resolved' (result has their
    answer).
    """
    return _run("check_maintainer_request", item_id=item_id)


@_tool()
def update_maintainer_inbox_item(item_id: str, status: Literal["acknowledged", "resolved"] | None = None, result: str | None = None) -> str:
    """Maintainer-only: acknowledge or resolve an inbox item. Use 'acknowledged'
    once you've seen it and are working it, 'resolved' once done — pass
    result with the answer/outcome so the flagging agent can retrieve it via
    check_maintainer_request without the user relaying it.
    """
    return _run("update_maintainer_inbox_item", item_id=item_id, status=status, result=result)


# Marked read-only only for Codex's approval gate: it changes nothing itself,
# it only files a proposal in the service's state for the user to answer.
@_tool(annotations=_READ_ONLY)
def propose_to_owner(kind: Literal["vault_note", "message_agent", "task"], title: str, why: str, content: str,
                     target: str | None = None, mode: Literal["append", "create"] | None = None,
                     agent: str | None = None, source: str | None = None) -> str:
    """Manager only. Propose something you want to do, for the user to answer Yes /
    No / Discuss on your page; nothing happens until they say yes.
    kind=vault_note: write something worth keeping into the main vault
    (target = the note's vault path from search_vault; mode=append to an
    existing note (default) or create for a new note in an existing folder;
    content = the exact Markdown to add, distilled, with a source line;
    source = the agent file it came from). kind=message_agent: a message to
    another agent (agent = its name; content = the whole message).
    kind=task: anything else you'd do yourself (content = exactly what); on
    yes you're told to go ahead in your Audit conversation. Agent folders and
    confidential folders are never written.
    """
    return _run("propose_to_owner", kind=kind, title=title, why=why, content=content, target=target,
                mode=mode, agent=agent, source=source)


# Also marked read-only only for Codex's approval gate (see finish_onboarding
# below). Its only write is this agent's own reminders.json. At send time a
# fixed reminder runs nothing; a compose one runs this agent once, look-up
# only (reminders.py).
@_tool(annotations=_READ_ONLY)
def schedule_reminder(action: Literal["add", "list", "cancel"], message: str | None = None, at: str | None = None,
                      repeat: Literal["daily", "weekly", "monthly"] | None = None, reminder_id: str | None = None,
                      compose: bool = False) -> str:
    """Send the user a message later: a push notification to their phone, also posted
    in your 'Reminders' conversation, where they can reply. For reminders,
    follow-ups on a future event, and recurring notes (e.g. a weekly watering
    note). By default the message is sent exactly as you write it now, so
    make it self-contained. compose=true: message is an instruction you'll
    follow when it's due (e.g. 'check tomorrow's rain forecast; tell the user
    whether to water the tomatoes'); you then run once, may only look things
    up, and your reply is what they get. Use it only when the message depends
    on information you can't have now. at: 'YYYY-MM-DD HH:MM' in the configured local timezone;
    the current time is in each message's 'Now' line. repeat: leave out for a
    one-off. Schedule only what the user asked for or agreed to. action='list'
    shows what's scheduled (with ids); action='cancel' removes one by id.
    """
    return _run("schedule_reminder", action=action, message=message, at=at, repeat=repeat, reminder_id=reminder_id,
                compose=compose)


# Marked read-only only so Codex's headless approval gate lets it through
# (Codex blocks every MCP tool without read_only_hint). It writes nothing
# outside this agent's own config and is only registered while onboarding.
@_tool(annotations=_READ_ONLY)
def finish_onboarding(tools: list[str], tagline: str | None = None, avatar_svg: str | None = None,
                      topics: list[str] | None = None) -> str:
    """Onboarding only, and only after the user has confirmed your proposal: saves
    which optional tools you get and ends onboarding. Write your mission.md and
    persona.md first. Core tools are always included; don't list them.
    avatar_svg: optional avatar the user agreed to — one small SVG, viewBox 0 0 64 64,
    plain shapes, under 20 KB.
    topics: chat topics the user agreed to (usually one per domain, short
    names); created empty, in this order.
    """
    return _run("finish_onboarding", tools=tools, tagline=tagline, avatar_svg=avatar_svg, topics=topics)


if __name__ == "__main__":
    mcp.run(transport="stdio")
