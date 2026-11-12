"""Workspace files (the owner's Files tab) and the agent's dashboard page.
Part of vault.py's facade: import vault, not this module."""

import json
import os
from datetime import datetime
from pathlib import Path

import vault
from agents import AGENTS
from vault_paths import _write_bytes_unlocked, _write_text_unlocked, dashboard_file, file_lock, write_text
from vault_notes import NoteConflictError, _RESERVED_NOTE_NAMES, _note_version
from vault_identity import IDENTITY_CHAR_LIMITS


# ── Workspace files (the owner editing from the agent page's Files tab) ──
# Unlike notes above, this covers the structured files too (plan, backlog,
# mission, persona, memory/…): it's the owner's direct edit, same as the Control
# Panel's mission/persona editor. conversations/ has its own tab, and
# dashboard.html is left out because a bad edit there would take down the
# very page you'd use to fix it.
_WORKSPACE_EXTENSIONS = {".md", ".txt", ".json", ".csv", ".yaml", ".yml"}
_WORKSPACE_SKIP_DIRS = {"conversations"}
MAX_WORKSPACE_FILE_BYTES = 512 * 1024
# Files the service or the agent's own tools depend on by name: editable in
# the Files tab, but not renamed, moved or deleted from it.
_PROTECTED_WORKSPACE_FILES = _RESERVED_NOTE_NAMES | {"kanban.json", "agent.json", "response_times.json", "reminders.json", "tasks.json"}


def is_protected_workspace_file(rel_path: str) -> bool:
    p = Path(rel_path)
    return len(p.parts) == 1 and p.name.lower() in _PROTECTED_WORKSPACE_FILES


def _unprotected_workspace_file(agent_id: str, rel_path: str) -> Path:
    if is_protected_workspace_file(rel_path):
        raise ValueError(f"{rel_path} is a core file for this agent and can't be renamed, moved or deleted")
    return workspace_file(agent_id, rel_path)


def handover_file(agent_id: str, rel_path: str) -> Path:
    """A file that can move to another agent (handover.py): not a core file."""
    return _unprotected_workspace_file(agent_id, rel_path)


def workspace_file(agent_id: str, rel_path: str) -> Path:
    if not rel_path or rel_path.startswith(("/", "~")):
        raise ValueError("invalid file path")
    p = Path(rel_path)
    if p.is_absolute() or any(part in ("", "..") or part.startswith(".") for part in p.parts):
        raise ValueError("invalid file path")
    if p.suffix.lower() not in _WORKSPACE_EXTENSIONS:
        raise ValueError("only text files (" + ", ".join(sorted(_WORKSPACE_EXTENSIONS)) + ") can be edited here")
    if p.parts[0] in _WORKSPACE_SKIP_DIRS:
        raise ValueError(f"{p.parts[0]}/ is edited from its own tab")
    root = vault.agent_root(agent_id)
    f = (root / p).resolve()
    if root not in f.parents:
        raise ValueError("invalid file path")
    return f


def list_workspace_files(agent_id: str) -> list[dict]:
    root = vault.agent_root(agent_id)
    out = []
    for f in root.rglob("*"):
        rel = f.relative_to(root)
        if not f.is_file() or f.suffix.lower() not in _WORKSPACE_EXTENSIONS:
            continue
        if rel.parts[0] in _WORKSPACE_SKIP_DIRS or any(part.startswith(".") for part in rel.parts):
            continue
        try:
            st = f.stat()
        except FileNotFoundError:
            continue
        out.append({"path": rel.as_posix(), "size": st.st_size,
                    "protected": is_protected_workspace_file(rel.as_posix()),
                    "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    # Top-level files first, then folders, alphabetical within each.
    out.sort(key=lambda e: ("/" in e["path"], e["path"].lower()))
    return out


def read_workspace_file(agent_id: str, rel_path: str) -> dict | None:
    f = workspace_file(agent_id, rel_path)
    if not f.is_file():
        return None
    if f.stat().st_size > MAX_WORKSPACE_FILE_BYTES:
        raise ValueError("file is too large to edit here")
    content = f.read_text(encoding="utf-8", errors="replace")
    return {"path": rel_path, "content": content, "version": _note_version(content)}


def create_workspace_file(agent_id: str, rel_path: str) -> dict:
    """New empty file from the Files tab. No extension means .md."""
    rel_path = rel_path.strip()
    if rel_path and not Path(rel_path).suffix:
        rel_path += ".md"
    f = workspace_file(agent_id, rel_path)
    with file_lock(agent_id, f) as scoped:
        if scoped.exists():
            raise FileExistsError(rel_path)
        _write_text_unlocked(scoped, "{}" if scoped.suffix.lower() == ".json" else "")
    return read_workspace_file(agent_id, rel_path)


def write_workspace_file(agent_id: str, rel_path: str, content: str, expected_version: str | None) -> dict:
    """Overwrite an existing file. Raises NoteConflictError if it changed
    since it was loaded (e.g. the agent wrote to it mid-edit)."""
    f = workspace_file(agent_id, rel_path)
    if len(content.encode("utf-8")) > MAX_WORKSPACE_FILE_BYTES:
        raise ValueError("file is too large to save here")
    if f.suffix.lower() == ".json":
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"not valid JSON: {exc}")
    if f.parent == vault.agent_root(agent_id) and f.stem in IDENTITY_CHAR_LIMITS and f.suffix == ".md":
        limit = IDENTITY_CHAR_LIMITS[f.stem]
        if len(content) > limit:
            raise ValueError(f"{f.stem.title()} is {len(content):,} characters; the limit is {limit:,}.")
    with file_lock(agent_id, f) as scoped:
        if not scoped.is_file():
            raise FileNotFoundError(rel_path)
        current = _note_version(scoped.read_text(encoding="utf-8", errors="replace"))
        if expected_version != current:
            raise NoteConflictError(f"{rel_path} changed since you opened it — reload to see the new version")
        _write_text_unlocked(scoped, content)
    return {"path": rel_path, "content": content, "version": _note_version(content)}


def rename_workspace_file(agent_id: str, rel_path: str, new_path: str) -> dict:
    """No extension on new_path keeps the old one."""
    new_path = new_path.strip()
    if new_path and not Path(new_path).suffix:
        new_path += Path(rel_path).suffix
    src = _unprotected_workspace_file(agent_id, rel_path)
    dst = _unprotected_workspace_file(agent_id, new_path)
    if src == dst:
        raise ValueError("that's the same name")
    with file_lock(agent_id, src) as scoped_src, file_lock(agent_id, dst) as scoped_dst:
        if not scoped_src.is_file():
            raise FileNotFoundError(rel_path)
        if scoped_dst.exists():
            raise FileExistsError(new_path)
        scoped_dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(scoped_src, scoped_dst)
    return {"path": new_path}


def delete_workspace_file(agent_id: str, rel_path: str) -> None:
    f = _unprotected_workspace_file(agent_id, rel_path)
    with file_lock(agent_id, f) as scoped:
        if not scoped.is_file():
            raise FileNotFoundError(rel_path)
        scoped.unlink()


def move_workspace_file(source_agent: str, target_agent: str, rel_path: str) -> dict:
    """To the same relative path in another agent's folder."""
    if target_agent not in AGENTS or source_agent == target_agent:
        raise ValueError("choose a different valid target agent")
    src = _unprotected_workspace_file(source_agent, rel_path)
    dst = _unprotected_workspace_file(target_agent, rel_path)
    with file_lock(source_agent, src) as scoped_src, file_lock(target_agent, dst) as scoped_dst:
        if not scoped_src.is_file():
            raise FileNotFoundError(rel_path)
        if scoped_dst.exists():
            raise FileExistsError(f"{AGENTS[target_agent].name} already has a file at {rel_path}")
        # Copy first, then remove the source. A failed copy leaves the original intact.
        _write_bytes_unlocked(scoped_dst, scoped_src.read_bytes())
        scoped_src.unlink()
    return {"path": rel_path, "source_agent": source_agent, "target_agent": target_agent}


# ── Dashboard (agent's own page) ───────────────────────────────────────

def read_dashboard(agent_id: str) -> str:
    f = dashboard_file(agent_id)
    return f.read_text(encoding="utf-8") if f.exists() else ""


def write_dashboard(agent_id: str, html: str) -> None:
    """Overwrites the agent's own dashboard page. The previous version is kept
    as a single backup (not a history) so a bad rewrite has a one-step revert."""
    f = dashboard_file(agent_id)
    if f.exists():
        backup = f.parent / (f.name + ".bak")
        backup.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    write_text(agent_id, f, html)
