"""Plan, backlog and the kanban stages on top of backlog items. Part of
vault.py's facade: import vault, not this module."""

import json
import re
from datetime import datetime
from pathlib import Path

import vault
from agents import AGENTS
from vault_paths import _backlog_lock, _write_text_unlocked, backlog_file, plan_file, render_frontmatter, split_frontmatter, write_text


# ── Plan ────────────────────────────────────────────────────────────────

def read_plan(agent_id: str) -> dict:
    f = plan_file(agent_id)
    if not f.exists():
        return {"status": "unknown", "updated": "", "target_event": "", "phases": [], "notes": ""}
    fm, body = split_frontmatter(f.read_text(encoding="utf-8"))
    return {
        "status": fm.get("Status", "unknown"),
        "updated": str(fm.get("Updated", "")),
        "target_event": fm.get("Target-event", ""),
        "phases": fm.get("Phases", []),
        "notes": body.strip(),
    }


def update_plan_phase(agent_id: str, name: str, percent: int, status: str | None = None) -> None:
    f = plan_file(agent_id)
    fm, body = split_frontmatter(f.read_text(encoding="utf-8")) if f.exists() else ({}, "")
    phases = fm.get("Phases", [])
    for p in phases:
        if p.get("name", "").lower() == name.lower():
            p["percent"] = percent
            if status:
                p["status"] = status
            break
    else:
        phases.append({"name": name, "status": status or "in-progress", "percent": percent})
    fm["Phases"] = phases
    fm["Updated"] = datetime.now().strftime("%Y-%m-%d")
    write_text(agent_id, f, render_frontmatter(fm, body))


# ── Backlog ─────────────────────────────────────────────────────────────

_CHECKBOX_RE = re.compile(r"^- \[( |x)\] (.+)$", re.M)


def read_backlog(agent_id: str) -> dict:
    f = backlog_file(agent_id)
    if not f.exists():
        return {"updated": "", "items": []}
    fm, body = split_frontmatter(f.read_text(encoding="utf-8"))
    items = [{"text": text, "done": mark == "x"} for mark, text in _CHECKBOX_RE.findall(body)]
    return {"updated": str(fm.get("Updated", "")), "items": items}


def backlog_add(agent_id: str, item: str) -> None:
    with _backlog_lock(agent_id) as f:
        title = "OpenClave" if f == vault.CONFIG.project_backlog_file else AGENTS[agent_id].name
        fm, body = split_frontmatter(f.read_text(encoding="utf-8")) if f.exists() else ({}, f"# {title} — Backlog\n")
        body = body.rstrip("\n") + f"\n- [ ] {item.strip()}\n"
        fm["Updated"] = datetime.now().strftime("%Y-%m-%d")
        _write_text_unlocked(f, render_frontmatter(fm, body))


def backlog_set_done(agent_id: str, item_substring: str, done: bool) -> bool:
    with _backlog_lock(agent_id) as f:
        fm, body = split_frontmatter(f.read_text(encoding="utf-8")) if f.exists() else ({}, "")
        mark = "x" if done else " "
        changed = False

        def repl(m):
            nonlocal changed
            if item_substring.lower() in m.group(2).lower():
                changed = True
                return f"- [{mark}] {m.group(2)}"
            return m.group(0)

        body = _CHECKBOX_RE.sub(repl, body)
        if changed:
            fm["Updated"] = datetime.now().strftime("%Y-%m-%d")
            _write_text_unlocked(f, render_frontmatter(fm, body))
        return changed


# ── Kanban (dashboard-only flagging layer on top of backlog items) ──────
#
# Stored separately from backlog.md itself (a sidecar JSON, not a marker
# embedded in the checkbox lines) so backlog.md stays the plain markdown
# checklist agents/the owner already read and hand-edit via update_backlog's
# add/check/uncheck, unaffected by dashboard flagging. Keyed by exact item
# text — same identity convention backlog_set_done's substring match already
# relies on. Renaming a backlog item silently drops its kanban stage; an
# accepted tradeoff for staying file-simple rather than inventing item ids.

KANBAN_STAGES = ("backlog", "in-progress", "done")


def kanban_file(agent_id: str) -> Path:
    return vault.agent_root(agent_id) / "kanban.json"


def read_kanban(agent_id: str) -> dict[str, str]:
    f = kanban_file(agent_id)
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def set_kanban_stage(agent_id: str, item_text: str, stage: str | None) -> None:
    """stage=None unflags the item (removes it from the kanban board entirely)."""
    stages = read_kanban(agent_id)
    if stage is None:
        stages.pop(item_text, None)
    else:
        if stage not in KANBAN_STAGES:
            raise ValueError(f"unknown kanban stage: {stage}")
        stages[item_text] = stage
    write_text(agent_id, kanban_file(agent_id), json.dumps(stages, indent=2, ensure_ascii=False) + "\n")
