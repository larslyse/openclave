"""Vault paths and writes: roots, per-agent files, locks, write_text and its
scope check, front matter. Part of vault.py's facade: import vault, not this module."""

import json
import hashlib
import os
import re
import tempfile
import time
import fcntl
from contextlib import contextmanager
from pathlib import Path

import yaml

import vault
from agents import AGENTS, has_role


class WriteScopeError(PermissionError):
    """Raised when a write is attempted outside the calling agent's own subtree."""


def agent_root(agent_id: str) -> Path:
    return (vault.AGENTS_ROOT / AGENTS[agent_id].folder).resolve()


def conversations_dir(agent_id: str) -> Path:
    return vault.agent_root(agent_id) / "conversations"


def plan_file(agent_id: str) -> Path:
    return vault.agent_root(agent_id) / "plan.md"


def backlog_file(agent_id: str) -> Path:
    if vault.CONFIG.project_backlog_file and has_role(agent_id, "maintainer"):
        return vault.CONFIG.project_backlog_file
    return vault.agent_root(agent_id) / "backlog.md"


def mission_file(agent_id: str) -> Path:
    return vault.agent_root(agent_id) / "mission.md"


def persona_file(agent_id: str) -> Path:
    return vault.agent_root(agent_id) / "persona.md"


def response_times_file(agent_id: str) -> Path:
    return vault.agent_root(agent_id) / "response_times.json"


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
    return vault.CONFIG.shared_concept_file


def dashboard_file(agent_id: str) -> Path:
    return vault.agent_root(agent_id) / "dashboard.html"


def _scoped_path(agent_id: str, path: Path) -> Path:
    root = vault.agent_root(agent_id)
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
    if vault.CONFIG.project_backlog_file and has_role(agent_id, "maintainer"):
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
