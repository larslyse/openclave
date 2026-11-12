"""Agent identity files: mission and persona with their size limits and
proposals, feedback, and foundations. Part of vault.py's facade: import vault,
not this module."""

from datetime import datetime
from pathlib import Path

import vault
from agents import AGENTS
from vault_paths import _write_text_unlocked, file_lock, mission_file, render_frontmatter, split_frontmatter, write_text
from vault_tracking import _slug, _split_sections
from vault_inbox import flag_to_maintainer, read_maintainer_inbox


# ── Persona / mission (self-editable, whole-body replace) ────────────────

IDENTITY_CHAR_LIMITS = {"mission": 1850, "persona": 2200}


class IdentityLimitReached(ValueError):
    def __init__(self, kind: str, item_id: str):
        self.kind = kind
        self.item_id = item_id
        super().__init__(f"{kind} not stored: character limit reached. Sent to the maintainer (id: {item_id}).")


def _send_identity_proposal(agent_id: str, kind: str, proposed: str) -> None:
    limit = IDENTITY_CHAR_LIMITS[kind]
    item_id = flag_to_maintainer(
        agent_id,
        f"{kind.title()} update not stored: {len(proposed)} characters exceeds the {limit}-character limit. "
        "Review and consolidate this proposed full file before applying it.\n\n"
        f"```markdown\n{proposed}\n```",
        reason="identity_char_limit",
    )
    raise IdentityLimitReached(kind, item_id)


def identity_snapshot(agent_id: str) -> dict[str, str | None]:
    """Capture exact file contents before a provider with direct file access runs."""
    return {
        kind: path.read_text(encoding="utf-8") if path.exists() else None
        for kind, path in (("mission", mission_file(agent_id)), ("persona", vault.persona_file(agent_id)))
    }


def enforce_identity_limits(agent_id: str, before: dict[str, str | None]) -> None:
    """Undo over-limit direct edits, retaining the proposed file in the maintainer inbox."""
    for kind, path in (("mission", mission_file(agent_id)), ("persona", vault.persona_file(agent_id))):
        if not path.exists():
            continue
        proposed = path.read_text(encoding="utf-8")
        if proposed == before[kind] or len(proposed) <= IDENTITY_CHAR_LIMITS[kind]:
            continue
        with file_lock(agent_id, path):
            previous = before[kind]
            if previous is None:
                path.unlink()
            else:
                _write_text_unlocked(path, previous)
        try:
            _send_identity_proposal(agent_id, kind, proposed)
        except IdentityLimitReached:
            pass


def identity_limit_item_ids(agent_id: str) -> set[str]:
    return {
        item["id"] for item in read_maintainer_inbox()
        if item.get("from_agent") == agent_id and item.get("reason") == "identity_char_limit"
    }


def _replace_body(agent_id: str, f: Path, body: str) -> None:
    fm, _ = split_frontmatter(f.read_text(encoding="utf-8")) if f.exists() else ({}, "")
    proposed = render_frontmatter(fm, body.strip() + "\n")
    kind = "mission" if f.name == "mission.md" else "persona"
    if len(proposed) > IDENTITY_CHAR_LIMITS[kind]:
        _send_identity_proposal(agent_id, kind, proposed)
    write_text(agent_id, f, proposed)


def read_identity_body(agent_id: str, kind: str) -> dict:
    """Body (without frontmatter) plus how much of the limit the frontmatter uses."""
    f = mission_file(agent_id) if kind == "mission" else vault.persona_file(agent_id)
    fm, body = split_frontmatter(f.read_text(encoding="utf-8")) if f.exists() else ({}, "")
    overhead = len(render_frontmatter(fm, "")) if fm else 0
    return {"body": body.strip(), "limit": IDENTITY_CHAR_LIMITS[kind], "frontmatter_chars": overhead}


def set_identity_body_by_owner(agent_id: str, kind: str, body: str) -> None:
    """The user editing in the Control Panel: frontmatter kept, Updated bumped.
    Over the limit is refused outright (not routed to the maintainer inbox as an
    agent's own over-limit edit is)."""
    f = mission_file(agent_id) if kind == "mission" else vault.persona_file(agent_id)
    fm, _ = split_frontmatter(f.read_text(encoding="utf-8")) if f.exists() else ({}, "")
    if fm:
        fm["Updated"] = datetime.now().strftime("%Y-%m-%d")
    proposed = render_frontmatter(fm, body.strip() + "\n") if fm else body.strip() + "\n"
    if len(proposed) > IDENTITY_CHAR_LIMITS[kind]:
        raise ValueError(f"{kind.title()} is {len(proposed):,} characters; the limit is {IDENTITY_CHAR_LIMITS[kind]:,}.")
    write_text(agent_id, f, proposed)


def update_mission(agent_id: str, body: str) -> None:
    _replace_body(agent_id, mission_file(agent_id), body)


def update_persona(agent_id: str, body: str) -> None:
    _replace_body(agent_id, vault.persona_file(agent_id), body)


_FEEDBACK_SECTION = "Feedback"


def add_feedback(agent_id: str, entry: str) -> None:
    """Appends one dated bullet to persona.md's '## Feedback' section — a
    running log of interaction-style corrections/confirmations from the user,
    kept separate from the free-prose personality body above it (only
    touched by the rare, full-rewrite update_persona) so one correction can
    never accidentally wipe the rest of who the agent is. Simple dated
    bullets, not a heavier structured format. Reuses the same
    section-split helpers as tracking.md/foundations.md."""
    f = vault.persona_file(agent_id)
    if f.exists():
        fm, body = split_frontmatter(f.read_text(encoding="utf-8"))
    else:
        fm, body = {}, ""
    top_prefix, sections = _split_sections(body)
    if top_prefix and not top_prefix.endswith("\n\n"):
        top_prefix = top_prefix.rstrip("\n") + "\n\n"
    idx = next((i for i, (t, _) in enumerate(sections) if _slug(t) == _slug(_FEEDBACK_SECTION)), None)
    date = datetime.now().strftime("%Y-%m-%d")
    line = f"- {entry.strip()} (added {date})\n"
    if idx is None:
        sections.append((_FEEDBACK_SECTION, f"## {_FEEDBACK_SECTION}\n\n{line}"))
    else:
        title, text = sections[idx]
        sections[idx] = (title, text.rstrip("\n") + "\n" + line)
    body = top_prefix + "\n\n".join(text.rstrip("\n") for _, text in sections) + "\n"
    fm["Updated"] = date
    proposed = render_frontmatter(fm, body)
    if len(proposed) > IDENTITY_CHAR_LIMITS["persona"]:
        _send_identity_proposal(agent_id, "persona", proposed)
    write_text(agent_id, f, proposed)


# ── Foundations (optional decision basis: claims and operating rules) ──

def foundations_file(agent_id: str) -> Path:
    return vault.agent_root(agent_id) / "foundations.md"


_FOUNDATIONS_TABLE_HEADER = "| Kind | Foundation | Confidence | Basis / reference |\n|---|---|---|---|\n"


def read_foundations(agent_id: str, topic: str | None = None) -> str:
    """Every topic's section concatenated if topic is omitted, otherwise just
    that one section's body."""
    f = foundations_file(agent_id)
    if not f.exists():
        return ""
    _, body = split_frontmatter(f.read_text(encoding="utf-8"))
    if not topic:
        return body.strip()
    _, sections = _split_sections(body)
    for title, text in sections:
        if _slug(title) == _slug(topic):
            return text.strip()
    return ""


def add_foundation(agent_id: str, topic: str, foundation: str, kind: str,
                   reference: str, confidence: int | None = None) -> None:
    if kind not in {"claim", "operating_rule"}:
        raise ValueError("kind must be claim or operating_rule")
    if not foundation.strip() or not reference.strip():
        raise ValueError("foundation and its source or rationale are required")
    if confidence is not None and (type(confidence) is not int or not 1 <= confidence <= 10):
        raise ValueError("confidence must be an integer from 1 to 10, or omitted")
    if kind == "operating_rule" and confidence is not None:
        raise ValueError("operating rules use rationale, not a confidence score")
    f = foundations_file(agent_id)
    with file_lock(agent_id, f) as scoped:
        if scoped.exists():
            fm, body = split_frontmatter(scoped.read_text(encoding="utf-8"))
        else:
            fm, body = {}, f"# {AGENTS[agent_id].name} — Foundations\n\n"
        top_prefix, sections = _split_sections(body)
        if top_prefix:
            top_prefix = top_prefix.rstrip() + "\n\n"
        title = " ".join(topic.strip().replace("_", " ").split()).title() or "General"
        idx = next((i for i, (t, _) in enumerate(sections) if _slug(t) == _slug(title)), None)
        def cell(value):
            return str(value).replace("|", "&#124;").replace("\n", " ").replace("\r", " ").strip()
        certainty = f"{confidence}/10" if confidence is not None else ("Not scored" if kind == "claim" else "—")
        label = "Evidence-based claim" if kind == "claim" else "Operating rule"
        row = f"| {label} | {cell(foundation)} | {certainty} | {cell(reference)} |\n"
        if idx is None:
            sections.append((title, f"## {title}\n\n{_FOUNDATIONS_TABLE_HEADER}{row}"))
        else:
            t, text = sections[idx]
            # Start a table after prose rather than appending a broken table row.
            last_line = text.rstrip().splitlines()[-1]
            if not last_line.startswith("|"):
                text = text.rstrip() + "\n\n" + _FOUNDATIONS_TABLE_HEADER
            sections[idx] = (t, text.rstrip("\n") + "\n" + row)
        body = top_prefix + "\n\n".join(text.rstrip("\n") for _, text in sections) + "\n"
        fm["Updated"] = datetime.now().strftime("%Y-%m-%d")
        scoped.parent.mkdir(parents=True, exist_ok=True)
        scoped.write_text(render_frontmatter(fm, body), encoding="utf-8")
