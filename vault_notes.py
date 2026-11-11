"""Generic agent notes and the memory folder. Part of vault.py's facade:
import vault, not this module."""

import hashlib
import re
from datetime import datetime
from pathlib import Path

import vault
from vault_paths import _write_text_unlocked, file_lock, render_frontmatter, split_frontmatter, write_text


# ── Generic notes (scoped, freeform, outside the structured files below) ──
# Backlog gap: agents previously had no way to create their own files (Reba
# wanted programs.md, Rocky/Stash need per-project/inventory files) without a
# bespoke tool per use case. Reserved names/dirs stay carved out because they
# already have dedicated tools with their own parsing/format guarantees —
# this must not become a second way to write dashboard.html or backlog.md.
class NoteConflictError(RuntimeError):
    pass


_NOTE_EXTENSIONS = {".md", ".txt"}
_RESERVED_NOTE_NAMES = {
    "dashboard.html", "dashboard.html.bak", "backlog.md", "plan.md", "mission.md",
    "persona.md", "foundations.md", "principles.md", "tracking.md", "inbox.md", "openclave.md",
}
_RESERVED_NOTE_DIRS = {"conversations", "memory"}


def _validate_note_path(rel_path: str) -> Path:
    if not rel_path or rel_path.startswith("/") or rel_path.startswith("~"):
        raise ValueError("invalid note path")
    p = Path(rel_path)
    if ".." in p.parts or p.is_absolute():
        raise ValueError("invalid note path")
    if p.suffix.lower() not in _NOTE_EXTENSIONS:
        raise ValueError("note path must end in .md or .txt")
    if p.parts[0] in _RESERVED_NOTE_DIRS:
        raise ValueError(f"{p.parts[0]}/ is reserved for its own tool")
    if len(p.parts) == 1 and p.name.lower() in _RESERVED_NOTE_NAMES:
        raise ValueError(f"{p.name} is reserved — use its dedicated tool instead")
    return p


def _note_version(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:12]


def note_file(agent_id: str, rel_path: str) -> Path:
    rel = _validate_note_path(rel_path)
    root = vault.agent_root(agent_id)
    f = (root / rel).resolve()
    if root not in f.parents:
        raise ValueError("invalid note path")
    return f


def read_note(agent_id: str, rel_path: str) -> dict | None:
    f = note_file(agent_id, rel_path)
    if not f.exists():
        return None
    content = f.read_text(encoding="utf-8")
    return {"content": content, "version": _note_version(content)}


def write_note(agent_id: str, rel_path: str, content: str, expected_version: str | None = None) -> str:
    """Returns the new version string. Raises NoteConflictError if the note
    already exists and expected_version is missing or stale — the caller must
    read_note first so a rewrite can't silently clobber an unseen change."""
    f = note_file(agent_id, rel_path)
    with file_lock(agent_id, f) as scoped:
        if scoped.exists():
            current_version = _note_version(scoped.read_text(encoding="utf-8"))
            if expected_version != current_version:
                raise NoteConflictError(
                    f"{rel_path} already exists (version {current_version}) — "
                    "read_note it first and pass its version to overwrite"
                )
        _write_text_unlocked(scoped, content)
    return _note_version(content)


def list_notes(agent_id: str) -> list[dict]:
    root = vault.agent_root(agent_id)
    out = []
    for f in root.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in _NOTE_EXTENSIONS:
            continue
        rel = f.relative_to(root)
        if rel.parts[0] in _RESERVED_NOTE_DIRS:
            continue
        if len(rel.parts) == 1 and rel.name.lower() in _RESERVED_NOTE_NAMES:
            continue
        try:
            modified = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
        except FileNotFoundError:
            continue
        out.append({"path": str(rel), "modified": modified})
    out.sort(key=lambda e: e["modified"], reverse=True)
    return out


# ── Memory (freeform, low-priority, not auto-loaded into the system prompt)
#
# Renamed from `notes` 2026-09-14/15 to match the folder the owner actually
# renamed it to on disk (both agents already had `memory/`, not `notes/` —
# this was previously a live path bug: write_note would have silently
# recreated an orphaned `notes/` folder on its next call). This is
# deliberately the LOWEST-priority bucket: genuinely one-off, "might matter
# later" content that doesn't need to be in every context window. Durable
# protocol/equipment facts belong in mission.md; a dated qualitative
# check-in belongs in tracking.md's 'Check-ins' section — see openclave.md's
# "What goes where" for the full classification. Misusing this bucket for
# something that should always inform recommendations is exactly the bug
# that motivated writing that rule down (2026-09-15: a slant-block rehab
# protocol note landed here instead of mission.md, and a check-in's
# qualitative narrative had nowhere to go because tracking.md had no
# free-text section yet).

def memory_dir(agent_id: str) -> Path:
    return vault.agent_root(agent_id) / "memory"


def list_memory(agent_id: str) -> list[dict]:
    d = memory_dir(agent_id)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.md"), reverse=True):
        fm, _ = split_frontmatter(f.read_text(encoding="utf-8"))
        out.append({"id": f.stem, "title": fm.get("Title", f.stem), "date": fm.get("Date", "")})
    return out


def read_memory(agent_id: str, entry_id: str) -> str | None:
    f = memory_dir(agent_id) / f"{entry_id}.md"
    if not f.exists():
        return None
    _, body = split_frontmatter(f.read_text(encoding="utf-8"))
    return body.strip()


def write_memory(agent_id: str, title: str, body: str) -> str:
    """Creates a new memory entry (timestamp + title slug, so titles never collide)."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "note"
    entry_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug}"
    fm = {"Title": title, "Date": datetime.now().strftime("%Y-%m-%d")}
    write_text(agent_id, memory_dir(agent_id) / f"{entry_id}.md", render_frontmatter(fm, body.strip() + "\n"))
    return entry_id
