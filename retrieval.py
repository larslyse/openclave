"""Pulls the relevant context slice for a chat message — QMD-backed, not a full-folder dump."""
import re
import sqlite3
from pathlib import Path

from agents import AGENTS

import service_settings
import vault

AUTO_LOOKUP_MAX_HITS = 5
KEYWORD_COUNT = 2
QMD_DB = Path(vault.QMD_ENV["XDG_CACHE_HOME"]) / "qmd" / f"{vault.QMD_INDEX}.sqlite"


def build_context(message: str) -> str:
    """Automatic note lookup before a text message, per the Control Panel's
    auto_lookup setting (service_settings.py). Conversation logs and archived
    design notes are always skipped (_EXCLUDED_PATHS): the message has usually
    just been saved to a conversation, so the search would find the chat itself
    (often the only hit for a message is that same message)."""
    mode = service_settings.load()["auto_lookup"]
    if mode == "off":
        return "(automatic lookup is off — use search_vault if you need context)"
    if not vault.qmd_available():
        return f"(vault search is off — qmd executable '{vault.QMD_BIN}' was not found)"
    query = message if mode == "full" else " ".join(message_keywords(message))
    if not query.strip():
        return "(no additional context retrieved)"
    hits = _drop_excluded_hits(vault.qmd_query(query, max_results=AUTO_LOOKUP_MAX_HITS * 2))
    return "\n\n".join(hits[:AUTO_LOOKUP_MAX_HITS]) or "(no additional context retrieved)"


# Never auto-injected: conversation logs (the chat would find itself) and
# archived OpenClave design notes (stale architecture).
# Both stay reachable through the agent's own search_vault tool.
_EXCLUDED_PATHS = ("/conversations/", "/openclave/build-docs/archive/")


def _drop_excluded_hits(output: str) -> list[str]:
    blocks = [b.strip() for b in re.split(r"(?m)^(?=qmd://)", output) if b.strip().startswith("qmd://")]
    blocks = vault.allowed_qmd_blocks(blocks)  # confidential folders, state, secrets
    return [b for b in blocks if not any(p in b.split("\n", 1)[0] for p in _EXCLUDED_PATHS)]


def message_keywords(message: str, count: int = KEYWORD_COUNT) -> list[str]:
    """The message's rarest words that still occur in the vault, ranked by how
    many notes contain them (the index's own full-text table) — "dunes" (4
    notes) beats "today" (28). No LLM call: ~5ms. Falls back to the longest
    words if the index can't be read."""
    words = list(dict.fromkeys(w.lower() for w in re.findall(r"[A-Za-z\u00C0-\u024F0-9]{3,}", message)))
    if not words:
        return []
    try:
        with sqlite3.connect(f"file:{QMD_DB}?mode=ro", uri=True) as db:
            freq = {w: db.execute("SELECT count(*) FROM documents_fts WHERE documents_fts MATCH ?",
                                  (f'"{w}"',)).fetchone()[0] for w in words}
    except sqlite3.Error:
        return sorted(words, key=len, reverse=True)[:count]
    present = sorted((w for w in words if freq[w] > 0), key=lambda w: freq[w])
    return present[:count]


def mission_text(agent_id: str) -> str:
    f = vault.mission_file(agent_id)
    if f.exists():
        _, body = vault.split_frontmatter(f.read_text(encoding="utf-8"))
        return body.strip()
    return "(no mission on file yet)"


def context_directory(agent_id: str) -> str:
    """Pointers and intent-based lookup rules; do not read the deferred notes."""
    return (
        "Use read_vault_file with these exact paths when relevant:\n"
        f"- Mission: {vault.mission_file(agent_id)} — read before personalized advice or "
        "decisions that depend on the user's circumstances and constraints.\n"
        f"- Plan: {vault.plan_file(agent_id)} — read for current progress, priorities, "
        "planning, or plan changes.\n"
        f"- Backlog: {vault.backlog_file(agent_id)} — read for outstanding work, "
        "task selection, or backlog changes.\n"
        f"- Foundations (optional): {vault.foundations_file(agent_id)} — use get_foundations(topic) "
        "when existing claims or agreed operating rules could affect a decision. "
        "A missing file is normal; do not create filler or treat absence as a blocker.\n"
        "Choose lookups by the intent of the request, not just matching keywords. "
        "Greetings and general questions do not require these reads. Read current "
        "records before updating them; never replace a mission from a partial excerpt. "
        "Do not invent missing context or assume a previous turn's lookup is still current. "
        "If required context is unavailable, state the gap."
    )


def concept_text() -> str:
    """Load the curated runtime section, leaving the full rationale retrievable.

    Unmarked notes retain the legacy behavior. Read fresh so vault edits apply
    immediately; never truncate an instruction just to meet a token budget.
    """
    f = vault.concept_file()
    if f.exists():
        _, body = vault.split_frontmatter(f.read_text(encoding="utf-8"))
        start = "<!-- openclave-runtime:start -->"
        end = "<!-- openclave-runtime:end -->"
        if start in body:
            _, _, remaining = body.partition(start)
            runtime, closing, _ = remaining.partition(end)
            if closing and runtime.strip():
                return runtime.strip()
        return body.strip()
    return "(no concept note on file yet)"


def persona_text(agent_id: str) -> str:
    """Source of truth for who this agent is — a vault note the user can read and
    tweak directly, not a string buried in agents.py."""
    f = vault.persona_file(agent_id)
    if f.exists():
        _, body = vault.split_frontmatter(f.read_text(encoding="utf-8"))
        return body.strip()
    return "(no persona on file — behave as a generic OpenClave agent until one is written)"


def plan_summary(agent_id: str) -> str:
    plan = vault.read_plan(agent_id)
    lines = [f"Target event: {plan['target_event']}", f"Plan status: {plan['status']}"]
    for p in plan["phases"]:
        lines.append(f"- {p.get('name')}: {p.get('status')} ({p.get('percent')}%)")
    return "\n".join(lines)


def agent_roster_text(agent_id: str) -> str:
    """Short, vault-owned routing descriptions; full domains remain retrievable.

    Fall back to complete domains for agents without a curated Routing field.
    """
    lines = []
    for aid, cfg in AGENTS.items():
        persona = vault.persona_file(aid)
        metadata = vault.split_frontmatter(persona.read_text(encoding="utf-8"))[0] if persona.exists() else {}
        routing = metadata.get("Routing")
        if isinstance(routing, str) and routing.strip():
            domains = routing.strip()
        else:
            domains = ", ".join(vault.agent_domains(aid)) or "(no domains listed)"
        who = " (you)" if aid == agent_id else ""
        lines.append(f"- {cfg.name}{who}: {domains}")
    return "\n".join(lines)


def foundations_text(agent_id: str) -> str:
    text = vault.read_foundations(agent_id)
    return text if text else "(no foundations recorded yet)"


def backlog_summary(agent_id: str) -> str:
    backlog = vault.read_backlog(agent_id)
    if not backlog["items"]:
        return "(backlog empty)"
    return "\n".join(f"- [{'x' if i['done'] else ' '}] {i['text']}" for i in backlog["items"])
