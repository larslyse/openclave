"""Vault access: read anywhere, write within the calling agent's folder,
except for the configured maintainer project backlog.

Write-scoping is enforced here at the code level (not by convention) — same
logic as before, now parameterized by agent_id instead of duplicated per agent.
"""
import json
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import fcntl

import yaml

from config import CONFIG
from agents import AGENTS, has_role

VAULT_ROOT = CONFIG.vault_root
AGENTS_ROOT = CONFIG.agents_root

QMD_INDEX = CONFIG.qmd_index
# Absolute path, not "qmd": a bare command name depends on PATH resolving it,
# which doesn't hold everywhere this subprocess call runs from. Same
# reasoning as CLAUDE_BIN/CODEX_BIN in llm_client.py.
QMD_BIN = CONFIG.qmd_bin
# qmd resolves its index location from XDG_CACHE_HOME/XDG_CONFIG_HOME rather
# than command-line flags. Pass the configured values explicitly so CLI child
# processes use the same index as the service.
QMD_ENV = CONFIG.qmd_env


class WriteScopeError(PermissionError):
    """Raised when a write is attempted outside the calling agent's own subtree."""


def agent_root(agent_id: str) -> Path:
    return (AGENTS_ROOT / AGENTS[agent_id].folder).resolve()


def conversations_dir(agent_id: str) -> Path:
    return agent_root(agent_id) / "conversations"


def plan_file(agent_id: str) -> Path:
    return agent_root(agent_id) / "plan.md"


def backlog_file(agent_id: str) -> Path:
    if CONFIG.project_backlog_file and has_role(agent_id, "maintainer"):
        return CONFIG.project_backlog_file
    return agent_root(agent_id) / "backlog.md"


def mission_file(agent_id: str) -> Path:
    return agent_root(agent_id) / "mission.md"


def persona_file(agent_id: str) -> Path:
    return agent_root(agent_id) / "persona.md"


def response_times_file(agent_id: str) -> Path:
    return agent_root(agent_id) / "response_times.json"


def record_response_time(agent_id: str, model_id: str, duration_ms: int) -> None:
    """Keep only the last five successful provider durations per model."""
    if duration_ms < 0:
        return
    path = response_times_file(agent_id)
    with file_lock(agent_id, path) as scoped:
        try:
            samples = json.loads(scoped.read_text(encoding="utf-8")) if scoped.exists() else {}
        except (OSError, ValueError):
            samples = {}
        recent = samples.get(model_id, [])
        if not isinstance(recent, list):
            recent = []
        samples[model_id] = (recent + [duration_ms])[-5:]
        _write_text_unlocked(scoped, json.dumps(samples, indent=2) + "\n")


def read_response_time_averages(agent_id: str) -> dict[str, dict[str, int]]:
    path = response_times_file(agent_id)
    try:
        samples = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        model_id: {"average_ms": round(sum(values) / len(values)), "samples": len(values)}
        for model_id, values in samples.items()
        if isinstance(values, list) and values and all(type(v) is int and v >= 0 for v in values)
    }


_DOMAINS_SECTION_RE = re.compile(r"^##\s*Domains\s*\n((?:-.*\n?)+)", re.M)


def agent_domains(agent_id: str) -> list[str]:
    """Expertise tags — read from a `## Domains` bullet list in mission.md, not
    hardcoded in agents.py, so the user can see and edit them alongside the rest of
    an agent's mission/context instead of hunting through Python."""
    f = mission_file(agent_id)
    if not f.exists():
        return []
    _, body = split_frontmatter(f.read_text(encoding="utf-8"))
    m = _DOMAINS_SECTION_RE.search(body)
    if not m:
        return []
    return [line.strip()[1:].strip() for line in m.group(1).strip().splitlines() if line.strip()]


def concept_file() -> Path:
    """Shared, non-agent-specific: what OpenClave is and why. Lives one level
    above Agents/ since it describes the whole project, not one agent.
    Renamed from concept.md -> openclave.md 2026-09-14; this must track that
    filename or every agent silently loses its shared context."""
    return CONFIG.shared_concept_file


def dashboard_file(agent_id: str) -> Path:
    return agent_root(agent_id) / "dashboard.html"


def _scoped_path(agent_id: str, path: Path) -> Path:
    root = agent_root(agent_id)
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise WriteScopeError(f"refused write outside {root}: {resolved}")
    return resolved


@contextmanager
def file_lock(agent_id: str, path: Path):
    """Cross-process advisory lock for a vault file's read-modify-write cycle."""
    scoped = _scoped_path(agent_id, path)
    lock_dir = Path(tempfile.gettempdir()) / "openclave-file-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(scoped).encode()).hexdigest()
    with open(lock_dir / f"{digest}.lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield scoped
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def _backlog_lock(agent_id: str):
    """Allow maintainers to edit only the configured shared backlog."""
    path = backlog_file(agent_id)
    if CONFIG.project_backlog_file and has_role(agent_id, "maintainer"):
        lock_dir = Path(tempfile.gettempdir()) / "openclave-file-locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(str(path).encode()).hexdigest()
        with open(lock_dir / f"{digest}.lock", "a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield path
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    else:
        with file_lock(agent_id, path) as scoped:
            yield scoped


def _write_text_unlocked(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp:
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _write_keeping_mtime(path: Path, content: str) -> None:
    """Rewrite without changing mtime. A conversation's mtime is its "last
    activity" (list_conversations, latest_activity), which drives the mobile
    "new" light: a rename must not look like a fresh reply."""
    mtime = path.stat().st_mtime_ns
    _write_text_unlocked(path, content)
    os.utime(path, ns=(time.time_ns(), mtime))


def _write_bytes_unlocked(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as temp:
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def write_text(agent_id: str, path: Path, content: str) -> None:
    with file_lock(agent_id, path) as scoped:
        _write_text_unlocked(scoped, content)


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
    root = agent_root(agent_id)
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
    root = agent_root(agent_id)
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
    root = agent_root(agent_id)
    f = (root / p).resolve()
    if root not in f.parents:
        raise ValueError("invalid file path")
    return f


def list_workspace_files(agent_id: str) -> list[dict]:
    root = agent_root(agent_id)
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
    if f.parent == agent_root(agent_id) and f.stem in IDENTITY_CHAR_LIMITS and f.suffix == ".md":
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


def read_text_anywhere_in_vault(path: Path) -> str:
    """Reads are allowed anywhere under the vault root (whole-vault read access)."""
    resolved = path.resolve()
    if resolved != VAULT_ROOT and VAULT_ROOT not in resolved.parents:
        raise PermissionError(f"path outside vault: {resolved}")
    return resolved.read_text(encoding="utf-8")


def split_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            fm_raw = text[4:end]
            body = text[end + 4:].lstrip("\n")
            try:
                fm = yaml.safe_load(fm_raw) or {}
            except yaml.YAMLError:
                fm = {}
            return fm, body
    return {}, text


def render_frontmatter(fm: dict, body: str) -> str:
    fm_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm_yaml}\n---\n\n{body}"


# ── Conversations ────────────────────────────────────────────────────────

_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def conversation_file(agent_id: str, conversation_id: str) -> Path:
    if not _CONVERSATION_ID_RE.fullmatch(conversation_id):
        raise ValueError("invalid conversation id")
    d = conversations_dir(agent_id).resolve()
    f = (d / f"{conversation_id}.md").resolve()
    if f.parent != d:
        raise ValueError("invalid conversation id")
    return f

def list_conversations(agent_id: str) -> list[dict]:
    d = conversations_dir(agent_id)
    if not d.exists():
        return []
    # IDs encode creation time, while the useful order is latest activity.
    # append_turn() atomically replaces the file and therefore updates mtime.
    files_with_mtime = []
    for f in d.glob("*.md"):
        try:
            files_with_mtime.append((f.stat().st_mtime_ns, f.name, f))
        except FileNotFoundError:
            # A concurrent delete can occur between glob() and stat().
            continue
    out = []
    for mtime, _, f in sorted(files_with_mtime, reverse=True):
        fm, body = split_frontmatter(f.read_text(encoding="utf-8"))
        headings = _LAST_HEADING_RE.findall(body)
        out.append({
            "id": f.stem,
            "title": fm.get("Title") or "New chat",
            "date": fm.get("Date", ""),
            # folders.py: the folder this chat is in, if any.
            "folder": str(fm["Folder"]) if fm.get("Folder") else None,
            # For the mobile "new" light per conversation.
            "updated_ms": mtime // 1_000_000,
            "last_is_reply": bool(headings) and headings[-1] != "User",
            # Set on another agent's "From <agent>" conversation (agent_messages.py).
            "from_agent": fm.get("From-agent") or None,
        })
    return out


_TIMING_COMMENT_RE = re.compile(r"<!-- openclave-timing [^\n]*-->\n?")
_TURN_HEADING_RE = re.compile(r"^### [^\n]*\n?", re.M)


def search_conversations(agent_id: str, query: str, limit: int = 30) -> list[dict]:
    """Conversations whose title or text contains every word of the query
    (case-insensitive), most recent first, each with a short snippet around
    the first match. Plain text scan: an agent has at most a few hundred."""
    words = [w for w in query.lower().split() if w][:8]
    if not words:
        return []
    out = []
    for convo in list_conversations(agent_id):
        f = conversation_file(agent_id, convo["id"])
        try:
            _, body = split_frontmatter(f.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        text = _TURN_HEADING_RE.sub("", _TIMING_COMMENT_RE.sub("", body))
        haystack = f"{convo['title']}\n{text}".lower()
        if not all(w in haystack for w in words):
            continue
        at = text.lower().find(words[0])
        if at < 0:
            snippet = ""
        else:
            start = max(0, at - 60)
            snippet = ("…" if start else "") + " ".join(text[start:at + 100].split()) + ("…" if at + 100 < len(text) else "")
        out.append({**convo, "snippet": snippet})
        if len(out) >= limit:
            break
    return out


_LAST_HEADING_RE = re.compile(r"^### (.+?) \(", re.M)


def latest_activity(agent_id: str) -> dict:
    """When this agent's most recently touched conversation last changed, and
    whether its last turn is the agent's (a reply the user may not have read).
    For the mobile overview's unread tag and recency sort."""
    d = conversations_dir(agent_id)
    newest = None
    for f in d.glob("*.md") if d.exists() else ():
        try:
            mtime = f.stat().st_mtime_ns
        except FileNotFoundError:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, f)
    if newest is None:
        return {"last_activity_ms": None, "last_is_reply": False, "last_conversation_id": None}
    try:
        headings = _LAST_HEADING_RE.findall(newest[1].read_text(encoding="utf-8"))
    except FileNotFoundError:
        headings = []
    return {"last_activity_ms": newest[0] // 1_000_000,
            "last_is_reply": bool(headings) and headings[-1] != "User",
            "last_conversation_id": newest[1].stem}


_FROM_RE = re.compile(r'^<!-- openclave-from (\{[^\n]*\}) -->\n?')
_SUMMARY_RE = re.compile(r"^### Summary \(([^)]*)\)\n(.*?)(?=\n### |\Z)", re.S | re.M)


def _turn_pattern(agent_name: str, prior_agents: list[str] | None = None) -> re.Pattern:
    names = [agent_name] + [name for name in (prior_agents or []) if name != agent_name]
    labels = "|".join(re.escape(name) for name in names)
    return re.compile(
        rf"^### (User|{labels}) \(([^)]+)\)\n(.*?)(?=\n### (?:User|{labels}) \(|\Z)",
        re.S | re.M,
    )


def _speaker_names(agent_id: str, fm: dict) -> list[str]:
    """Other labels an agent's turns may carry: agents a moved conversation
    came from, and this agent's own names before a rename."""
    return list(fm.get("Prior-agents", [])) + list(AGENTS[agent_id].former_names)


def _raw_turns(body: str, agent_name: str, prior_agents: list[str] | None = None) -> list[dict]:
    """Turns with their exact source markdown preserved (m.group(0)), so a
    'kept' turn can be spliced back into a rewritten file byte-for-byte
    rather than re-rendered from parsed fields."""
    out = []
    for m in _turn_pattern(agent_name, prior_agents).finditer(body):
        role, ts, content = m.groups()
        content = content.strip()
        elapsed_ms = None
        model_id = None
        voice = False
        # Metadata is kept in markdown for durability, but excluded from
        # parsed conversation text. Older turns need no migration.
        from_agent = None
        if role == "User":
            # A message another agent sent (agent_messages.py / room.py).
            sender = _FROM_RE.match(content)
            if sender:
                try:
                    value = json.loads(sender.group(1)).get("agent")
                    if isinstance(value, str) and len(value) <= 64:
                        from_agent = value
                        content = content[sender.end():].strip()
                except (ValueError, AttributeError):
                    pass
        if role != "User":
            timing = re.match(r'^<!-- openclave-timing (\{[^\n]*\}) -->\n', content)
            if timing:
                try:
                    meta = json.loads(timing.group(1))
                    value = meta.get("elapsed_ms")
                    if type(value) is int and value >= 0:
                        elapsed_ms = value
                        content = content[timing.end():].strip()
                    # Which model answered (recorded since 2026-09-27).
                    if isinstance(meta.get("model_id"), str) and len(meta["model_id"]) <= 80:
                        model_id = meta["model_id"]
                    # Voice turns answer on the fixed voice model, not the agent's.
                    voice = "synthesize_ms" in meta
                except (ValueError, AttributeError):
                    pass
        out.append({"role": "user" if role == "User" else "assistant", "ts": ts, "content": content, "elapsed_ms": elapsed_ms, "model_id": model_id, "voice": voice, "from_agent": from_agent, "raw": m.group(0)})
    return out


def get_conversation(agent_id: str, conversation_id: str, archived: bool = False) -> dict | None:
    """archived=True reads it from conversations/archived/ (the Recaps tab's
    "Read the chat")."""
    f = archived_conversation_file(agent_id, conversation_id) if archived else conversation_file(agent_id, conversation_id)
    if not f.exists():
        return None
    fm, body = split_frontmatter(f.read_text(encoding="utf-8"))
    agent_name = AGENTS[agent_id].name
    summary_match = _SUMMARY_RE.search(body)
    return {
        "id": conversation_id,
        "title": fm.get("Title") or "New chat",
        "date": fm.get("Date", ""),
        "folder": str(fm["Folder"]) if fm.get("Folder") else None,
        "purged": bool(fm.get("Purged")),  # purge.py: the chat can be restored
        "turns": [
            {
                "role": t["role"], "content": t["content"], "ts": t["ts"],
                "elapsed_ms": t["elapsed_ms"],
                "model_id": t["model_id"],
                "voice": t["voice"],
                "from_agent": t["from_agent"],
                "attachments": turn_attachments(agent_id, conversation_id, t["content"]),
            }
            for t in _raw_turns(body, agent_name, _speaker_names(agent_id, fm))
        ],
        "summary": summary_match.group(2).strip() if summary_match else None,
    }


def conversation_size(agent_id: str, conversation_id: str) -> int:
    f = conversation_file(agent_id, conversation_id)
    return len(f.read_text(encoding="utf-8")) if f.exists() else 0


def raw_turn_blocks(agent_id: str, conversation_id: str) -> list[dict]:
    """Like get_conversation()['turns'] but keeps each turn's exact source
    text (the 'raw' field) — needed by compaction to splice kept turns back
    in unchanged rather than re-rendering them from parsed content."""
    f = conversation_file(agent_id, conversation_id)
    if not f.exists():
        return []
    fm, body = split_frontmatter(f.read_text(encoding="utf-8"))
    return _raw_turns(body, AGENTS[agent_id].name, _speaker_names(agent_id, fm))


def compact_conversation(agent_id: str, conversation_id: str, summary: str, keep_raw_turns: list[str]) -> str:
    """Replaces everything before the kept recent turns with one synthetic
    '### Summary (...)' block. The full pre-compaction file is archived first
    (conversations/archive/) so nothing is silently, untraceably lost — same
    lineage-preservation idea as Hermes Agent's compaction design. Returns the
    archive filename."""
    f = conversation_file(agent_id, conversation_id)
    with file_lock(agent_id, f) as scoped:
        original = scoped.read_text(encoding="utf-8")
        fm, _ = split_frontmatter(original)

        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        archive_name = f"{conversation_id}-pre-compaction-{ts}.md"
        write_text(agent_id, conversations_dir(agent_id) / "archive" / archive_name, original)

        date = datetime.now().strftime("%Y-%m-%d")
        summary_block = f"### Summary ({date}, full history archived at conversations/archive/{archive_name})\n{summary.strip()}\n"
        new_body = summary_block + "\n" + "".join(keep_raw_turns)
        fm["Compacted"] = date
        _write_text_unlocked(scoped, render_frontmatter(fm, new_body.strip() + "\n"))
    return archive_name


def append_turn(agent_id: str, conversation_id: str, role: str, content: str, first_message: str | None = None, *, timings: dict | None = None, sender: str | None = None) -> None:
    agent_name = AGENTS[agent_id].name
    f = conversation_file(agent_id, conversation_id)
    now = datetime.now()
    ts = now.strftime("%H:%M")
    with file_lock(agent_id, f) as scoped:
        title = ((first_message or content).strip().splitlines() or [""])[0][:60]
        if scoped.exists():
            fm, body = split_frontmatter(scoped.read_text(encoding="utf-8"))
            # A chat created empty (create_conversation with no title) is named
            # from its first message, like any other new chat.
            if not fm.get("Title") and role == "user" and title:
                fm["Title"] = title
        else:
            fm = {"Agent": agent_name, "Date": now.strftime("%Y-%m-%d"), "Title": title}
            body = ""
        label = "User" if role == "user" else agent_name
        metadata = f"<!-- openclave-timing {json.dumps(timings)} -->\n" if role != "user" and timings else ""
        if role == "user" and sender:
            metadata = f"<!-- openclave-from {json.dumps({'agent': sender})} -->\n"
        body += f"\n### {label} ({ts})\n{metadata}{content.strip()}\n"
        _write_text_unlocked(scoped, render_frontmatter(fm, body.strip() + "\n"))


def find_conversation(agent_id: str, key: str, value: str) -> str | None:
    """The newest conversation whose front matter has key: value, e.g. the
    "From-agent" conversation agent_messages.py delivers into."""
    for f in conversations_dir(agent_id).glob("*.md"):
        try:
            fm, _ = split_frontmatter(f.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        if str(fm.get(key, "")) == value:
            return f.stem
    return None


def create_conversation(agent_id: str, title: str, extra: dict | None = None) -> str:
    """An empty conversation with a fixed title. Auto-naming only replaces
    the starting title from a first message, so this title stays."""
    conversation_id = new_conversation_id()
    f = conversation_file(agent_id, conversation_id)
    fm = {"Agent": AGENTS[agent_id].name, "Date": datetime.now().strftime("%Y-%m-%d"), "Title": title, **(extra or {})}
    with file_lock(agent_id, f) as scoped:
        _write_text_unlocked(scoped, render_frontmatter(fm, ""))
    return conversation_id


def ensure_conversation(agent_id: str, conversation_id: str, title: str) -> None:
    """A fixed-id conversation with a fixed title (e.g. the manager's
    "Audit"), created empty if it isn't there yet."""
    f = conversation_file(agent_id, conversation_id)
    fm = {"Agent": AGENTS[agent_id].name, "Date": datetime.now().strftime("%Y-%m-%d"), "Title": title}
    with file_lock(agent_id, f) as scoped:
        if not scoped.exists():
            _write_text_unlocked(scoped, render_frontmatter(fm, ""))


def new_conversation_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def rename_conversation(agent_id: str, conversation_id: str, title: str) -> bool:
    """Same path-scoping as delete_conversation — pinned to the conversations
    dir so a crafted conversation_id can't target a sibling file."""
    f = conversation_file(agent_id, conversation_id)
    with file_lock(agent_id, f) as scoped:
        if not scoped.exists():
            return False
        fm, body = split_frontmatter(scoped.read_text(encoding="utf-8"))
        fm["Title"] = title.strip()
        _write_keeping_mtime(scoped, render_frontmatter(fm, body))
    return True


def rename_conversation_if(agent_id: str, conversation_id: str, expected: str, title: str) -> bool:
    """Rename only if the title is still `expected` (auto-naming must never
    overwrite a name the user gave meanwhile)."""
    f = conversation_file(agent_id, conversation_id)
    with file_lock(agent_id, f) as scoped:
        if not scoped.exists():
            return False
        fm, body = split_frontmatter(scoped.read_text(encoding="utf-8"))
        if fm.get("Title") != expected:
            return False
        fm["Title"] = title.strip()
        _write_keeping_mtime(scoped, render_frontmatter(fm, body))
    return True


def move_conversation(source_agent: str, target_agent: str, conversation_id: str) -> bool:
    """Move a transcript, its attachments and compaction archives together."""
    if target_agent not in AGENTS or source_agent == target_agent:
        raise ValueError("choose a different valid target agent")
    source = conversation_file(source_agent, conversation_id)
    target = conversation_file(target_agent, conversation_id)
    if not source.exists():
        return False
    if target.exists():
        raise FileExistsError("target agent already has a conversation with this id")
    source_attachments = attachments_dir(source_agent, conversation_id)
    target_attachments = attachments_dir(target_agent, conversation_id)
    source_archives = list((conversations_dir(source_agent) / "archive").glob(f"{conversation_id}-pre-compaction-*.md"))
    target_archive_dir = conversations_dir(target_agent) / "archive"
    if target_attachments.exists() or any((target_archive_dir / f.name).exists() for f in source_archives):
        raise FileExistsError("target already has attachments or archived history for this conversation")

    fm, body = split_frontmatter(source.read_text(encoding="utf-8"))
    prior = fm.get("Prior-agents", [])
    if not isinstance(prior, list):
        prior = [str(prior)] if prior else []
    for source_name in (AGENTS[source_agent].name, *AGENTS[source_agent].former_names):
        if source_name not in prior:
            prior.append(source_name)
    fm["Prior-agents"] = prior
    fm["Agent"] = AGENTS[target_agent].name

    # Copy first, then remove the source. A failed copy leaves the original intact.
    try:
        if source_attachments.exists():
            shutil.copytree(source_attachments, target_attachments)
        target_archive_dir.mkdir(parents=True, exist_ok=True)
        for archive in source_archives:
            shutil.copy2(archive, target_archive_dir / archive.name)
        write_text(target_agent, target, render_frontmatter(fm, body))
    except Exception:
        if target_attachments.exists():
            shutil.rmtree(target_attachments)
        for archive in source_archives:
            (target_archive_dir / archive.name).unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise
    source.unlink()
    if source_attachments.exists():
        shutil.rmtree(source_attachments)
    for archive in source_archives:
        archive.unlink()
    return True


# Archived conversations (weekly cleanup, 2026-10-13): moved aside, not
# deleted. Lists, search and the Files tab only read conversations/*.md, so
# they disappear from view; attachments stay where they are, so a restored
# conversation is whole again.
ARCHIVED_DIR = "archived"


def archived_conversation_file(agent_id: str, conversation_id: str) -> Path:
    live = conversation_file(agent_id, conversation_id)  # validates the id
    return live.parent / ARCHIVED_DIR / live.name


def archive_conversation(agent_id: str, conversation_id: str) -> bool:
    source = conversation_file(agent_id, conversation_id)
    target = archived_conversation_file(agent_id, conversation_id)
    with file_lock(agent_id, source) as scoped:
        if not scoped.exists():
            return False
        if target.exists():
            raise FileExistsError("an archived conversation with this id already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        scoped.rename(target)
    return True


def unarchive_conversation(agent_id: str, conversation_id: str) -> bool:
    source = archived_conversation_file(agent_id, conversation_id)
    target = conversation_file(agent_id, conversation_id)
    with file_lock(agent_id, target) as scoped:
        if not source.exists():
            return False
        if scoped.exists():
            raise FileExistsError("a conversation with this id already exists")
        source.rename(scoped)
    return True


def restore_if_archived(agent_id: str, conversation_id: str | None) -> bool:
    """A message to an archived chat (opened from Recaps) brings it back
    first. True if it was restored. Never raises for a missing one."""
    if not conversation_id:
        return False
    try:
        if conversation_file(agent_id, conversation_id).exists():
            return False
        if not archived_conversation_file(agent_id, conversation_id).exists():
            return False
        return unarchive_conversation(agent_id, conversation_id)
    except (ValueError, FileExistsError):
        return False


def list_archived_conversations(agent_id: str) -> list[dict]:
    d = conversations_dir(agent_id) / ARCHIVED_DIR
    out = []
    for f in sorted(d.glob("*.md") if d.exists() else (), key=lambda f: f.stat().st_mtime, reverse=True):
        fm, _ = split_frontmatter(f.read_text(encoding="utf-8"))
        out.append({"id": f.stem, "title": fm.get("Title") or f.stem, "date": fm.get("Date", ""),
                    "folder": str(fm["Folder"]) if fm.get("Folder") else None})
    return out


def delete_conversation(agent_id: str, conversation_id: str) -> bool:
    """Pinned to the conversations dir specifically (not just agent_root) so a
    crafted conversation_id can't be used to delete a sibling file like
    mission.md or persona.md."""
    f = conversation_file(agent_id, conversation_id)
    with file_lock(agent_id, f) as scoped:
        if not scoped.exists():
            return False
        scoped.unlink()
    attach_dir = attachments_dir(agent_id, conversation_id)
    if attach_dir.exists():
        shutil.rmtree(attach_dir)
    return True


# ── Attachments (images pasted/uploaded into a conversation) ─────────────
#
# One conversation-scoped subfolder per attachment id, not a flat pool —
# keeps them alongside the conversation they belong to (delete_conversation
# above cleans this up too) and keeps the id-collision surface small (ids are
# a content hash, scoped per conversation, not globally unique).
#
# Bytes and metadata are separate files (<id>.bin / <id>.json) rather than
# guessing a file extension back from media_type on read — simpler and keeps
# the original filename/size available without re-deriving them.

_ATTACHMENT_ID_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
# Documents (2026-10-04: every agent must take attachments). Their text
# is extracted and added to the message (attachment_text), so they work on
# every model; images go to the model as images.
DOCUMENT_MEDIA_TYPES = {"application/pdf", "text/plain", "text/markdown", "text/csv", "application/json"}
ATTACHMENT_MEDIA_TYPES = IMAGE_MEDIA_TYPES | DOCUMENT_MEDIA_TYPES
MAX_DOCUMENT_TEXT_CHARS = 60_000
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # raw upload cap; clients are expected to downscale before this


def attachments_dir(agent_id: str, conversation_id: str) -> Path:
    # Reuses conversation_file's id validation, without requiring the
    # conversation to already exist — an attachment can be uploaded before
    # the first message that references it.
    conversation_file(agent_id, conversation_id)
    return conversations_dir(agent_id) / "attachments" / conversation_id


def _attachment_base(agent_id: str, conversation_id: str, attachment_id: str) -> Path:
    if not _ATTACHMENT_ID_RE.fullmatch(attachment_id):
        raise ValueError("invalid attachment id")
    d = attachments_dir(agent_id, conversation_id).resolve()
    base = (d / attachment_id).resolve()
    if base.parent != d:
        raise ValueError("invalid attachment id")
    return base


def save_attachment(agent_id: str, conversation_id: str, filename: str, media_type: str, data: bytes) -> dict:
    if media_type not in ATTACHMENT_MEDIA_TYPES:
        raise ValueError(f"unsupported media type: {media_type}")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"attachment too large: {len(data)} bytes (max {MAX_ATTACHMENT_BYTES})")
    attachment_id = hashlib.sha256(data).hexdigest()[:16]
    base = _attachment_base(agent_id, conversation_id, attachment_id)
    meta = {"id": attachment_id, "filename": filename, "media_type": media_type, "size": len(data)}
    data_file, meta_file = base.with_suffix(".bin"), base.with_suffix(".json")
    with file_lock(agent_id, data_file):
        if not data_file.exists():
            _write_bytes_unlocked(data_file, data)
            _write_text_unlocked(meta_file, json.dumps(meta))
    return meta


def read_attachment(agent_id: str, conversation_id: str, attachment_id: str) -> tuple[bytes, str] | None:
    meta = get_attachment_meta(agent_id, conversation_id, attachment_id)
    if meta is None:
        return None
    base = _attachment_base(agent_id, conversation_id, attachment_id)
    return base.with_suffix(".bin").read_bytes(), meta["media_type"]


def get_attachment_meta(agent_id: str, conversation_id: str, attachment_id: str) -> dict | None:
    try:
        base = _attachment_base(agent_id, conversation_id, attachment_id)
    except ValueError:
        return None
    meta_file = base.with_suffix(".json")
    if not meta_file.exists():
        return None
    return json.loads(meta_file.read_text(encoding="utf-8"))


def attachment_markdown_ref(conversation_id: str, meta: dict) -> str:
    bang = "!" if meta.get("media_type") in IMAGE_MEDIA_TYPES else ""
    return f"{bang}[{meta['filename']}](attachments/{conversation_id}/{meta['id']})"


def attachment_text(agent_id: str, conversation_id: str, attachment_id: str) -> str:
    """A document attachment's text for the model (PDF pages, or the file as
    UTF-8), capped at MAX_DOCUMENT_TEXT_CHARS. Never raises: says what went
    wrong instead, so the agent can tell the user."""
    found = read_attachment(agent_id, conversation_id, attachment_id)
    if not found:
        return "(file not found)"
    data, media_type = found
    try:
        if media_type == "application/pdf":
            import io
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(data))
            text = "\n\n".join(f"[page {i + 1}]\n{(page.extract_text() or '').strip()}" for i, page in enumerate(reader.pages))
            if not re.search(r"\w", re.sub(r"\[page \d+\]", "", text)):
                return f"(a {len(reader.pages)}-page PDF with no extractable text: probably scanned images)"
        else:
            text = data.decode("utf-8", errors="replace")
    except Exception as exc:
        return f"(couldn't read this file: {type(exc).__name__})"
    if len(text) > MAX_DOCUMENT_TEXT_CHARS:
        text = text[:MAX_DOCUMENT_TEXT_CHARS] + f"\n… (cut here: the file has {len(text):,} characters)"
    return text


_ATTACHMENT_REF_RE = re.compile(r"!?\[[^\]]*\]\(attachments/[^/]+/([A-Za-z0-9]+)\)")


def turn_attachments(agent_id: str, conversation_id: str, content: str) -> list[dict]:
    out = []
    for attachment_id in _ATTACHMENT_REF_RE.findall(content):
        meta = get_attachment_meta(agent_id, conversation_id, attachment_id)
        if meta:
            out.append(meta)
    return out


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
        for kind, path in (("mission", mission_file(agent_id)), ("persona", persona_file(agent_id)))
    }


def enforce_identity_limits(agent_id: str, before: dict[str, str | None]) -> None:
    """Undo over-limit direct edits, retaining the proposed file in the maintainer inbox."""
    for kind, path in (("mission", mission_file(agent_id)), ("persona", persona_file(agent_id))):
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
    f = mission_file(agent_id) if kind == "mission" else persona_file(agent_id)
    fm, body = split_frontmatter(f.read_text(encoding="utf-8")) if f.exists() else ({}, "")
    overhead = len(render_frontmatter(fm, "")) if fm else 0
    return {"body": body.strip(), "limit": IDENTITY_CHAR_LIMITS[kind], "frontmatter_chars": overhead}


def set_identity_body_by_lars(agent_id: str, kind: str, body: str) -> None:
    """The user editing in the Control Panel: frontmatter kept, Updated bumped.
    Over the limit is refused outright (not routed to the maintainer inbox as an
    agent's own over-limit edit is)."""
    f = mission_file(agent_id) if kind == "mission" else persona_file(agent_id)
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
    _replace_body(agent_id, persona_file(agent_id), body)


_FEEDBACK_SECTION = "Feedback"


def add_feedback(agent_id: str, entry: str) -> None:
    """Appends one dated bullet to persona.md's '## Feedback' section — a
    running log of interaction-style corrections/confirmations from the user,
    kept separate from the free-prose personality body above it (only
    touched by the rare, full-rewrite update_persona) so one correction can
    never accidentally wipe the rest of who the agent is. Mirrors OpenClaw's
    own '## Cortex Preferences / Lessons' pattern in its MEMORY.md: simple
    dated bullets, not a heavier structured format. Reuses the same
    section-split helpers as tracking.md/foundations.md."""
    f = persona_file(agent_id)
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
        title = "OpenClave" if f == CONFIG.project_backlog_file else AGENTS[agent_id].name
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


# ── Maintainer inbox — the one deliberate cross-folder write ────────────
#
# Any agent may flag an item here (bugs, issues, external-research requests,
# anything needing maintainer judgment) via flag_to_maintainer. This is the
# one cross-agent write path and always targets the configured maintainer's folder,
# which agent calls it, through the same write_text/file_lock scoping as
# everything else.
#
# Structured as one JSON file per item (same shape as chat_jobs.py's request
# records) rather than markdown checkbox lines, per Backlog.md item #5: a
# checkbox has no durable id, no room for a result, and "resolve item X" had
# to mean substring-matching checklist text. This gives every item an id,
# status, and an optional result the flagging agent can retrieve —
# update_maintainer_inbox_item is the maintainer's tool to acknowledge/resolve;
# check_maintainer_request is any agent's tool to poll the status/result of
# something they flagged, closing the "return a result to the requesting
# agent" gap without the owner acting as courier.

_INBOX_STATUSES = {"open", "acknowledged", "resolved"}


def maintainer_inbox_dir() -> Path | None:
    import agents
    maintainer = agents.maintainer_id()
    return agent_root(maintainer) / "inbox" if maintainer else None


def _maintainer_inbox_item_file(item_id: str) -> Path:
    if not _CONVERSATION_ID_RE.fullmatch(item_id):
        raise ValueError("invalid inbox item id")
    inbox = maintainer_inbox_dir()
    if inbox is None:
        raise ValueError("no agent has the maintainer role")
    d = inbox.resolve()
    f = (d / f"{item_id}.json").resolve()
    if f.parent != d:
        raise ValueError("invalid inbox item id")
    return f


def flag_to_maintainer(from_agent_id: str, summary: str, *, reason: str | None = None) -> str:
    """Returns the new item's id, so the flagging agent can poll it later."""
    item_id = new_conversation_id()
    from_name = AGENTS[from_agent_id].name if from_agent_id in AGENTS else from_agent_id
    now = datetime.now().isoformat()
    record = {
        "id": item_id,
        "from_agent": from_agent_id,
        "from_name": from_name,
        "summary": summary.strip(),
        "reason": reason,
        "status": "open",
        "result": None,
        "created_at": now,
        "updated_at": now,
    }
    import agents
    maintainer = agents.maintainer_id()
    if maintainer is None:
        raise RuntimeError("no agent has the maintainer role")
    write_text(maintainer, _maintainer_inbox_item_file(item_id), json.dumps(record, indent=2) + "\n")
    return item_id


def read_maintainer_inbox(status: str | None = None) -> list[dict]:
    d = maintainer_inbox_dir()
    if d is None or not d.exists():
        return []
    items = []
    for f in d.glob("*.json"):
        try:
            items.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    if status is not None:
        items = [i for i in items if i.get("status") == status]
    items.sort(key=lambda i: i.get("created_at", ""), reverse=True)
    return items


def get_maintainer_inbox_item(item_id: str) -> dict | None:
    try:
        f = _maintainer_inbox_item_file(item_id)
    except ValueError:
        return None
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def update_maintainer_inbox_item(item_id: str, status: str | None = None, result: str | None = None) -> bool:
    if status is not None and status not in _INBOX_STATUSES:
        raise ValueError(f"invalid status: {status} (must be one of {sorted(_INBOX_STATUSES)})")
    import agents
    maintainer = agents.maintainer_id()
    if maintainer is None:
        return False
    f = _maintainer_inbox_item_file(item_id)
    with file_lock(maintainer, f) as scoped:
        if not scoped.exists():
            return False
        record = json.loads(scoped.read_text(encoding="utf-8"))
        if status is not None:
            record["status"] = status
        if result is not None:
            record["result"] = result
        record["updated_at"] = datetime.now().isoformat()
        _write_text_unlocked(scoped, json.dumps(record, indent=2) + "\n")
    return True


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
    return agent_root(agent_id) / "kanban.json"


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


# ── Tracking (open-ended measurements: pain, ROM, rehab sessions, etc.) ──
#
# Stored in the body as one or more date x metric matrices, each under its own
# '## <Section>' heading (e.g. Progress, General Activities, Rehab Activities,
# Strength) — one file, multiple independent tables, rather than a separate
# file per topic, so the owner has one place to look. Same idea as backlog.md's
# checkbox list either way: a hidden YAML list in frontmatter is invisible in
# Obsidian's normal reading view and can't be corrected by hand, which defeats
# the point of a log the owner actually looks at. Columns within a section are
# discovered from whatever metric names have been logged there (preserving
# existing column order, new ones appended) so new kinds of tracked data don't
# need a schema change — just a consistent name reused call to call, and a
# consistent section name to keep it grouped with its own kind of matrix.
#
# Dates are displayed/stored DD-MM-YYYY (the owner's preference) but every other
# date in/out of this module (API responses, the log tool's `date` param) is
# ISO YYYY-MM-DD — the dashboard's own date sorting/parsing assumes ISO, so
# the DD-MM-YYYY conversion happens only at this file's read/write boundary.

def tracking_file(agent_id: str) -> Path:
    return agent_root(agent_id) / "tracking.md"


_DISPLAY_DATE_FMT = "%d-%m-%Y"
_ISO_DATE_FMT = "%Y-%m-%d"
_MATRIX_DATE_RE = re.compile(r"^\d{2}-\d{2}-\d{4}$")
_TRACKING_INSTRUCTIONS = "Edit values directly in the table below to correct a log — it's read on every dashboard load.\n\n"
DEFAULT_TRACKING_SECTION = "Progress"
_SECTION_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def _split_sections(body: str) -> tuple[str, list[tuple[str, str]]]:
    """Splits on '## ' headings into (text_before_first_heading, [(heading, raw_text_incl_heading), ...])."""
    matches = list(_SECTION_HEADING_RE.finditer(body))
    if not matches:
        return body, []
    top_prefix = body[:matches[0].start()]
    sections = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections.append((m.group(1).strip(), body[m.start():end]))
    return top_prefix, sections


def tracking_sections(agent_id: str) -> list[str]:
    """Return this agent's tracking headings, with a generic first-use default."""
    f = tracking_file(agent_id)
    if f.exists():
        _, body = split_frontmatter(f.read_text(encoding="utf-8"))
        _, sections = _split_sections(body)
        names = [title for title, _ in sections]
        if names:
            return names
    return [DEFAULT_TRACKING_SECTION]


def _to_display_date(iso_or_display: str) -> str:
    try:
        return datetime.strptime(iso_or_display, _ISO_DATE_FMT).strftime(_DISPLAY_DATE_FMT)
    except ValueError:
        return iso_or_display  # already display format (or malformed) — pass through


def _to_iso_date(display_or_iso: str) -> str:
    try:
        return datetime.strptime(display_or_iso, _DISPLAY_DATE_FMT).strftime(_ISO_DATE_FMT)
    except ValueError:
        return display_or_iso


def _format_cell(value: str | None, unit: str | None, note: str | None) -> str:
    # A note-only cell (no value — e.g. a Check-ins narrative, or a session
    # log with no single number) keeps an explicit leading "— " marker rather
    # than stripping it away, so _parse_cell can tell "no value, this is all
    # free text" apart from "a value happens to be a multi-word phrase" on
    # read. Without this, a pure-text note like "Sand dunes, 15kg backpack..."
    # silently mis-parsed as value="Sand", unit="dunes, 15kg backpack..." —
    # found 2026-09-15 checking the existing 'hike' entry, not new here.
    if not value:
        return f"— {note}" if note else ""
    s = value
    if unit:
        s = f"{s} {unit}".strip()
    if note:
        s = f"{s} — {note}".strip(" —")
    return s


def _parse_cell(cell: str) -> tuple[str | None, str | None, str | None]:
    cell = cell.strip()
    if not cell:
        return None, None, None
    if cell.startswith("— "):
        return None, None, cell[2:].strip() or None
    main, _, note = cell.partition(" — ")
    value, _, unit = main.strip().partition(" ")
    return (value or None), (unit.strip() or None), (note.strip() or None)


def _parse_tracking_matrix(body: str) -> tuple[str, list[str], dict[str, dict[str, str]]]:
    """Returns (prefix_text_before_table, columns, {display_date: {metric: cell}})."""
    lines = body.splitlines()
    header_idx = None
    columns: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|") and stripped.strip("|").split("|")[0].strip().lower() == "date":
            columns = [c.strip() for c in stripped.strip("|").split("|")][1:]
            header_idx = i
            break
    if header_idx is None:
        return body, [], {}
    prefix = "\n".join(lines[:header_idx])
    if prefix:
        prefix += "\n"
    rows: dict[str, dict[str, str]] = {}
    for line in lines[header_idx + 1:]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not cells or not _MATRIX_DATE_RE.match(cells[0]):
            continue  # separator row or junk — skip
        row = {col: val for col, val in zip(columns, cells[1:]) if val}
        rows[cells[0]] = row
    return prefix, columns, rows


def _render_tracking_matrix(columns: list[str], rows: dict[str, dict[str, str]]) -> str:
    header = "| Date | " + " | ".join(columns) + " |"
    sep = "|---|" + "|".join(["---"] * len(columns)) + "|"
    sorted_dates = sorted(rows.keys(), key=lambda d: datetime.strptime(d, _DISPLAY_DATE_FMT), reverse=True)
    lines = [header, sep]
    for d in sorted_dates:
        row = rows[d]
        lines.append("| " + d + " | " + " | ".join(row.get(c, "") for c in columns) + " |")
    return "\n".join(lines) + "\n"


def log_measurement(
    agent_id: str,
    metric: str,
    value: str | None,
    unit: str | None,
    note: str | None,
    on_date: str | None,
    section: str = DEFAULT_TRACKING_SECTION,
) -> None:
    log_measurements(agent_id, [{
        "metric": metric, "value": value, "unit": unit, "note": note, "date": on_date,
    }], section=section)


def log_measurements(agent_id: str, new_entries: list[dict], section: str = DEFAULT_TRACKING_SECTION) -> None:
    """Writes any number of entries into one section's date x metric matrix in
    one read-modify-write. Callers that need to log several metrics at once
    (e.g. the dashboard's Log button) MUST batch through here rather than
    firing one call per metric — separate concurrent calls each do their own
    read-modify-write on the same tracking.md with no locking, so parallel
    requests race and silently drop each other's entries."""
    f = tracking_file(agent_id)
    agent_name = AGENTS[agent_id].name
    if f.exists():
        fm, body = split_frontmatter(f.read_text(encoding="utf-8"))
    else:
        fm, body = {}, f"# {agent_name} — Tracking\n\n{_TRACKING_INSTRUCTIONS}"
    top_prefix, sections = _split_sections(body)
    idx = next((i for i, (t, _) in enumerate(sections) if _slug(t) == _slug(section)), None)
    if idx is None:
        sections.append((section, f"## {section}\n\n"))
        idx = len(sections) - 1
    title, section_text = sections[idx]
    prefix, columns, rows = _parse_tracking_matrix(section_text)
    today_iso = datetime.now().strftime(_ISO_DATE_FMT)
    for e in new_entries:
        date_disp = _to_display_date(e.get("date") or today_iso)
        metric = e["metric"]
        if metric not in columns:
            columns.append(metric)
        rows.setdefault(date_disp, {})[metric] = _format_cell(e.get("value"), e.get("unit"), e.get("note"))
    sections[idx] = (title, prefix + _render_tracking_matrix(columns, rows) + "\n")
    body = top_prefix + "".join(text for _, text in sections)
    fm["Updated"] = today_iso
    write_text(agent_id, f, render_frontmatter(fm, body))


def read_measurements(
    agent_id: str, metric: str | None = None, limit: int | None = None, section: str = DEFAULT_TRACKING_SECTION
) -> dict:
    f = tracking_file(agent_id)
    if not f.exists():
        return {"updated": "", "entries": []}
    fm, body = split_frontmatter(f.read_text(encoding="utf-8"))
    _, sections = _split_sections(body)
    idx = next((i for i, (t, _) in enumerate(sections) if _slug(t) == _slug(section)), None)
    if idx is None:
        return {"updated": str(fm.get("Updated", "")), "entries": []}
    _, section_text = sections[idx]
    _, columns, rows = _parse_tracking_matrix(section_text)
    entries = []
    for date_disp, row in rows.items():
        for met, cell in row.items():
            value, unit, note = _parse_cell(cell)
            entries.append({"date": _to_iso_date(date_disp), "metric": met, "value": value, "unit": unit, "note": note})
    if metric:
        entries = [e for e in entries if e.get("metric", "").lower() == metric.lower()]
    entries = sorted(entries, key=lambda e: e.get("date", ""), reverse=True)
    if limit:
        entries = entries[:limit]
    return {"updated": str(fm.get("Updated", "")), "entries": entries}


# ── Foundations (optional decision basis: claims and operating rules) ──

def foundations_file(agent_id: str) -> Path:
    return agent_root(agent_id) / "foundations.md"


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
    return agent_root(agent_id) / "memory"


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


# ── QMD retrieval ───────────────────────────────────────────────────────
#
# Uses BM25 keyword search ("qmd search"), not the hybrid "qmd query" with
# LLM-driven query expansion + reranking. This box has no GPU, and reranking
# alone measured ~370s for 40 candidates on CPU — "qmd query" was silently
# timing out on every chat message and falling back to empty context. BM25
# measured ~0.8s with genuinely relevant results, so it's the default here.
# Revisit if/when reranking runs somewhere with GPU acceleration.

_QMD_REFRESH_MIN_INTERVAL_S = 30
_qmd_last_refresh: float = 0.0


def _qmd_refresh_index(timeout: int = 15) -> None:
    """Best-effort file-index refresh (not embeddings — those are slow on this
    CPU-only box and only matter for the unused hybrid 'qmd query' path).
    Found the index 10 days stale on 2026-09-14, with search_vault blind to
    that entire session's new content as a result. Measured cost for this
    vault's size: ~0.4s for 265 files — cheap enough to run before every
    search rather than needing a separate cron/scheduling mechanism, so
    retrieval is self-healing instead of depending on remembering to run
    `qmd update` manually. Never raises: a failed/slow refresh should not
    block the chat response, just mean this one search runs on a
    slightly-stale index.

    Throttled to once per _QMD_REFRESH_MIN_INTERVAL_S: the staleness this
    guards against is hours/days of inactivity, not the seconds between
    consecutive messages in one conversation — profiled 2026-09-18 as a real,
    unconditional per-message cost with no benefit at that frequency. Still
    refreshes on the first message after any real idle gap."""
    global _qmd_last_refresh
    if not qmd_available():
        return
    now = time.monotonic()
    if now - _qmd_last_refresh < _QMD_REFRESH_MIN_INTERVAL_S:
        return
    try:
        _qmd_ensure_collection(timeout)
        subprocess.run(
            [QMD_BIN, "--index", QMD_INDEX, "update"],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **QMD_ENV},
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    _qmd_last_refresh = now


def _qmd_ensure_collection(timeout: int) -> None:
    """A new install has an empty qmd index: index the notes folder the first
    time, so search works without a manual `qmd collection add`."""
    env = {**os.environ, **QMD_ENV}
    listed = subprocess.run([QMD_BIN, "--index", QMD_INDEX, "collection", "list"],
                            capture_output=True, text=True, timeout=timeout, env=env)
    if "No collections" in (listed.stdout + listed.stderr):
        subprocess.run([QMD_BIN, "--index", QMD_INDEX, "collection", "add", str(VAULT_ROOT), "--name", "notes"],
                       capture_output=True, text=True, timeout=max(timeout, 120), env=env)


def qmd_query(query: str, timeout: int = 10, max_results: int | None = None) -> str:
    if not qmd_available():
        return f"(vault search is off — qmd executable '{QMD_BIN}' was not found)"
    _qmd_refresh_index()
    try:
        result = subprocess.run(
            [QMD_BIN, "--index", QMD_INDEX, "search", query, *(["-n", str(max_results)] if max_results else [])],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **QMD_ENV},
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


SEARCH_MAX_HITS = 12


def _qmd_blocks(output: str) -> list[str]:
    return [b.strip() for b in re.split(r"(?m)^(?=qmd://)", output or "") if b.strip().startswith("qmd://")]


def _hit_file(block: str) -> str:
    return re.split(r"[:\s]", block.split("\n", 1)[0][len("qmd://"):], maxsplit=1)[0]


def search_vault(query: str, max_hits: int = SEARCH_MAX_HITS) -> str:
    """The search_vault tool. qmd's keyword search needs every word to match
    and does not translate, so "travel insurance" missed a note called
    Forsikringsoversikt (2026-10-27). Alternatives split on "|" (e.g. the
    same words in another language) run as separate searches; a multi-word
    alternative with no hits is retried word by word. Results are merged
    round-robin, one block per file, conversation logs last."""
    alternatives = [a.strip() for a in query.split("|") if a.strip()] or [query.strip()]
    per_alt = []
    for alt in alternatives:
        blocks = _qmd_blocks(qmd_query(alt))
        words = alt.split()
        if not blocks and len(words) > 1:
            for word in words:
                if len(word) >= 3:
                    blocks += _qmd_blocks(qmd_query(word, max_results=3))
        per_alt.append(blocks)
    merged, seen = [], set()
    for rank in range(max(map(len, per_alt), default=0)):
        for blocks in per_alt:
            if rank < len(blocks) and _hit_file(blocks[rank]) not in seen:
                seen.add(_hit_file(blocks[rank]))
                merged.append(blocks[rank])
    merged.sort(key=lambda b: "/conversations/" in b.split("\n", 1)[0])  # stable: notes first
    return "\n\n".join(merged[:max_hits])


def search_language_hint() -> str:
    langs = CONFIG.note_languages
    if len(langs) < 2:
        return "Separate alternative searches with | (e.g. synonyms)."
    example = ("'travel insurance | reiseforsikring'" if "Norwegian" in langs
               else f"'<words> | <the same words in {langs[1]}>'")
    return (f"Notes are written in {', '.join(langs[:-1])} and {langs[-1]}, and the search does not translate: "
            f"always give the keywords in each language as alternatives separated by |, e.g. {example}.")


def qmd_get(path_or_uri: str, timeout: int = 10) -> str:
    """Full contents of one file, resolved by qmd itself (handles qmd:// URIs
    and its lowercased/dashed path convention) so callers don't have to guess
    the real on-disk path/casing."""
    # Context-directory paths must work even before QMD indexes a note, and
    # must reflect current edits. Keep URI/search-result resolution with QMD.
    if Path(path_or_uri).is_absolute():
        try:
            return read_text_anywhere_in_vault(Path(path_or_uri))
        except (OSError, ValueError) as exc:
            return f"(could not read '{path_or_uri}': {exc})"
    if not qmd_available():
        return f"(vault search is off — qmd executable '{QMD_BIN}' was not found; use an absolute vault path to read a file)"
    try:
        result = subprocess.run(
            [QMD_BIN, "--index", QMD_INDEX, "get", path_or_uri],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **QMD_ENV},
        )
        if result.returncode != 0:
            return f"(could not read '{path_or_uri}': {result.stderr.strip()[:200]})"
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return f"(timed out reading '{path_or_uri}')"


def qmd_available() -> bool:
    """Whether the configured QMD executable is available on this host."""
    return shutil.which(QMD_BIN) is not None


# ── The main vault (outside agent folders): manager proposals only ────────
# The manager's weekly audit proposes notes worth keeping in the owner's main
# vault (proposals.py); this writes one only after the owner said yes. It
# never touches agent folders, configured confidential folders or anything
# but Markdown; never creates folders and never deletes: it appends to an
# existing note or creates a new note in an existing folder.
class MainVaultError(ValueError):
    pass


_CHANGELOG_RE = re.compile(r"^## Changelog[ \t]*$", re.M)


def main_vault_target(rel_path: str) -> Path:
    """Resolve and check a vault-relative note path. Raises MainVaultError."""
    rel_path = (rel_path or "").strip().strip("/")
    if rel_path.startswith("qmd://"):
        rel_path = rel_path.split("/", 3)[-1]
    p = Path(rel_path)
    if not rel_path or p.is_absolute() or ".." in p.parts or rel_path.startswith("~"):
        raise MainVaultError("give the note's path inside the vault, like 'Area/Projects/Note.md'")
    if p.suffix.lower() != ".md":
        raise MainVaultError("only Markdown notes (.md)")
    f = (VAULT_ROOT / p).resolve()
    if VAULT_ROOT not in f.parents:
        raise MainVaultError("that path is outside the vault")
    agents_root = AGENTS_ROOT.resolve()
    if f == agents_root or agents_root in f.parents:
        raise MainVaultError("that's inside an agent's folder; propose a note in the main vault")
    for name in CONFIG.confidential_folders:
        folder = (VAULT_ROOT / name).resolve()
        if folder in f.parents:
            raise MainVaultError("that folder is confidential; nothing is written there")
    if any(part.startswith(".") for part in f.relative_to(VAULT_ROOT).parts):
        raise MainVaultError("hidden folders are off limits")
    return f


@contextmanager
def _main_vault_lock(path: Path):
    lock_dir = Path(tempfile.gettempdir()) / "openclave-file-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(path).encode()).hexdigest()
    with open(lock_dir / f"{digest}.lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def write_main_vault_note(rel_path: str, content: str, mode: str, change_summary: str) -> str:
    """mode 'append': add content to an existing note (before its
    '## Changelog', if it has one); 'create': a new note in an existing folder.
    A note with a '## Changelog' section gets a dated [BOT] line there.
    Returns the vault-relative path written."""
    f = main_vault_target(rel_path)
    content = (content or "").strip()
    if not content:
        raise MainVaultError("nothing to write")
    if mode not in ("append", "create"):
        raise MainVaultError("mode must be append or create")
    if not f.parent.is_dir():
        raise MainVaultError(f"the folder {f.parent.relative_to(VAULT_ROOT)} doesn't exist; new notes go in existing folders")
    stamp = f"- {datetime.now().strftime('%Y-%m-%d')} [BOT] {' '.join((change_summary or 'Added from OpenClave').split())[:200]}"
    with _main_vault_lock(f):
        if mode == "create":
            if f.exists():
                raise MainVaultError("that note already exists; propose an append instead")
            if content.startswith("---\n"):
                end = content.find("\n---", 4)
                try:
                    if end == -1 or not isinstance(yaml.safe_load(content[4:end]) or {}, dict):
                        raise MainVaultError("the new note's front matter isn't valid")
                except yaml.YAMLError as exc:
                    raise MainVaultError("the new note's front matter isn't valid YAML") from exc
            text = content + "\n"
            if _CHANGELOG_RE.search(text):
                text = text.rstrip("\n") + "\n" + stamp + "\n"
        else:
            if not f.exists():
                raise MainVaultError("that note doesn't exist; propose creating it instead")
            text = f.read_text(encoding="utf-8")
            m = _CHANGELOG_RE.search(text)
            if m:
                before, after = text[:m.start()].rstrip("\n"), text[m.start():].rstrip("\n")
                text = f"{before}\n\n{content}\n\n{after}\n{stamp}\n"
            else:
                text = text.rstrip("\n") + f"\n\n{content}\n"
        _write_text_unlocked(f, text)
    return str(f.relative_to(VAULT_ROOT))
