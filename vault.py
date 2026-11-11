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
import sqlite3
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


# ── Re-exports (the code lives in the vault_* modules) ──
from vault_paths import (  # noqa: F401
    WriteScopeError, _DOMAINS_SECTION_RE, _backlog_lock, _scoped_path, _write_bytes_unlocked,
    _write_keeping_mtime, _write_text_unlocked, agent_domains, agent_root, backlog_file,
    concept_file, conversations_dir, dashboard_file, file_lock, mission_file, persona_file,
    plan_file, read_response_time_averages, record_response_time, render_frontmatter,
    response_times_file, split_frontmatter, write_text,
)
from vault_guard import (  # noqa: F401
    VaultReadRefused, _HOME_SECRET_FILES, _SECRET_NAME_PREFIXES, _SECRET_SUFFIXES,
    _confidential_dirs, _secret_dirs, _secret_refusal, _within, confidential_refusal, read_refusal,
    read_text_anywhere_in_vault,
)
from vault_notes import (  # noqa: F401
    NoteConflictError, _NOTE_EXTENSIONS, _RESERVED_NOTE_DIRS, _RESERVED_NOTE_NAMES, _note_version,
    _validate_note_path, list_memory, list_notes, memory_dir, note_file, read_memory, read_note,
    write_memory, write_note,
)
from vault_tracking import (  # noqa: F401
    DEFAULT_TRACKING_SECTION, _DISPLAY_DATE_FMT, _ISO_DATE_FMT, _MATRIX_DATE_RE,
    _SECTION_HEADING_RE, _TRACKING_INSTRUCTIONS, _format_cell, _parse_cell, _parse_tracking_matrix,
    _render_tracking_matrix, _slug, _split_sections, _to_display_date, _to_iso_date,
    log_measurement, log_measurements, read_measurements, tracking_file, tracking_sections,
)
from vault_planning import (  # noqa: F401
    KANBAN_STAGES, _CHECKBOX_RE, backlog_add, backlog_set_done, kanban_file, read_backlog,
    read_kanban, read_plan, set_kanban_stage, update_plan_phase,
)
from vault_attachments import (  # noqa: F401
    ATTACHMENT_MEDIA_TYPES, DOCUMENT_MEDIA_TYPES, IMAGE_MEDIA_TYPES, MAX_ATTACHMENT_BYTES,
    MAX_DOCUMENT_TEXT_CHARS, _ATTACHMENT_ID_RE, _ATTACHMENT_REF_RE, _attachment_base,
    attachment_markdown_ref, attachment_text, attachments_dir, get_attachment_meta, read_attachment,
    save_attachment, turn_attachments,
)
from vault_conversations import (  # noqa: F401
    ARCHIVED_DIR, _CONVERSATION_ID_RE, _FROM_RE, _LAST_HEADING_RE, _SUMMARY_RE, _TIMING_COMMENT_RE,
    _TURN_HEADING_RE, _raw_turns, _speaker_names, _turn_pattern, append_turn, archive_conversation,
    archived_conversation_file, compact_conversation, conversation_file, conversation_size,
    create_conversation, delete_conversation, ensure_conversation, find_conversation,
    get_conversation, latest_activity, list_archived_conversations, list_conversations,
    move_conversation, new_conversation_id, raw_turn_blocks, rename_conversation,
    rename_conversation_if, restore_if_archived, search_conversations, unarchive_conversation,
)
from vault_inbox import (  # noqa: F401
    _INBOX_STATUSES, _maintainer_inbox_item_file, flag_to_maintainer, get_maintainer_inbox_item,
    maintainer_inbox_dir, read_maintainer_inbox, update_maintainer_inbox_item,
)
# @@END-REEXPORTS@@


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


def set_identity_body_by_owner(agent_id: str, kind: str, body: str) -> None:
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
    never accidentally wipe the rest of who the agent is. Simple dated
    bullets, not a heavier structured format. Reuses the same
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
        _qmd_apply_ignores()
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


# ── Keeping denied files out of qmd ───────────────────────────────────────
# qmd indexes **/*.md and already skips dotfiles and dot-folders. The state
# folder and confidential folders hold Markdown too, so they go into each
# collection's `ignore` list in qmd's YAML config; the next `qmd update` then
# drops them from the index. Because `qmd get` also resolves docids and
# path-suffix guesses, an empty index is what keeps them unreachable; the
# checks on search hits and get results below cover an index that has not
# been refreshed yet or a qmd that ignores the setting.

def _qmd_config_file() -> Path:
    return Path(QMD_ENV["XDG_CONFIG_HOME"]) / "qmd" / f"{QMD_INDEX}.yml"


def _qmd_db_file() -> Path:
    return Path(QMD_ENV["XDG_CACHE_HOME"]) / "qmd" / f"{QMD_INDEX}.sqlite"


def _denied_index_dirs() -> tuple[Path, ...]:
    return _secret_dirs() + tuple(folder for _, folder in _confidential_dirs())


def _glob_escape(text: str) -> str:
    return re.sub(r"([*?\[\]{}()!\\])", r"\\\1", text)


def _qmd_ignore_patterns(root: Path) -> list[str]:
    """fast-glob patterns, relative to a collection root, for the denied
    folders inside it ("**" when the root itself is inside one)."""
    patterns = []
    denied = _denied_index_dirs()
    for folder in denied:
        if _within(root, folder):
            return ["**"]
        if any(other in folder.parents for other in denied):
            continue  # already covered by the folder around it
        if root in folder.parents:
            patterns.append(_glob_escape(folder.relative_to(root).as_posix()) + "/**")
    return patterns


def _qmd_apply_ignores() -> None:
    """Add the denied folders to the ignore list of every collection in this
    index's qmd config, keeping any patterns already there. Never raises."""
    f = _qmd_config_file()
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return
    collections = data.get("collections") if isinstance(data, dict) else None
    if not isinstance(collections, dict):
        return
    changed = False
    for spec in collections.values():
        if not isinstance(spec, dict) or not spec.get("path"):
            continue
        current = spec.get("ignore") if isinstance(spec.get("ignore"), list) else []
        wanted = [p for p in _qmd_ignore_patterns(Path(str(spec["path"])).resolve()) if p not in current]
        if wanted:
            spec["ignore"] = [*current, *wanted]
            changed = True
    if changed:
        try:
            _write_text_unlocked(f, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        except OSError:
            pass


def _qmd_collection_roots() -> dict[str, Path]:
    try:
        data = yaml.safe_load(_qmd_config_file().read_text(encoding="utf-8")) or {}
        collections = data.get("collections") or {}
        return {str(name): Path(str(spec["path"])).resolve() for name, spec in collections.items()
                if isinstance(spec, dict) and spec.get("path")}
    except (OSError, yaml.YAMLError, AttributeError, TypeError):
        return {}


def _qmd_segment(text: str) -> str:
    """One path segment the way qmd stores it: lowercase, runs of anything
    but letters, digits and $ turned into a dash, no dashes at the ends."""
    return re.sub(r"(?:[^\w$]|_)+", "-", text.lower()).strip("-")


def _qmd_parts(path: Path | str) -> list[str]:
    return [s for s in (_qmd_segment(p) for p in Path(path).parts) if s]


def qmd_uri_refusal(uri: str, roots: dict[str, Path] | None = None) -> str | None:
    """read_refusal for a qmd document: "qmd://collection/path" (a ":line"
    suffix and anything after it are ignored) or "collection/path". The path
    is in qmd's lowercased, dashed form, so it is compared segment by segment
    in that form against the denied folders inside the collection's root."""
    text = (uri or "").strip().split()[0] if (uri or "").strip() else ""
    text = re.sub(r":\d+$", "", text.removeprefix("qmd://"))
    collection, _, rel = text.partition("/")
    if not rel:
        return None
    roots = _qmd_collection_roots() if roots is None else roots
    root = roots.get(collection, VAULT_ROOT.resolve())
    segments = [s for s in rel.split("/") if s]
    if any(s.startswith(".") for s in segments):
        return _secret_refusal(Path(rel))
    lowered = [_qmd_segment(s) for s in segments]
    confidential = dict((folder, name) for name, folder in _confidential_dirs())
    for folder in _denied_index_dirs():
        name = confidential.get(folder)
        if _within(root, folder):
            return confidential_refusal(name) if name else _secret_refusal(Path(rel))
        if root in folder.parents:
            prefix = _qmd_parts(folder.relative_to(root))
            if lowered[:len(prefix)] == prefix:
                return confidential_refusal(name) if name else _secret_refusal(Path(rel))
    home = CONFIG.home.resolve()
    if root in home.parents or root == home:
        for secret in _HOME_SECRET_FILES:
            if lowered == _qmd_parts((home / secret).relative_to(root)):
                return _secret_refusal(Path(rel))
    if segments and (Path(segments[-1]).suffix.lower() in _SECRET_SUFFIXES
                     or segments[-1].lower().startswith(_SECRET_NAME_PREFIXES)):
        return _secret_refusal(Path(rel))
    return None


def allowed_qmd_blocks(blocks: list[str]) -> list[str]:
    """Search-result blocks whose document agents may see."""
    roots = _qmd_collection_roots()
    return [b for b in blocks if qmd_uri_refusal(b.split("\n", 1)[0], roots) is None]


def _qmd_get_refusal(output: str) -> str | None:
    """For a `qmd get` result: the refusal when every indexed document with
    exactly this text is denied. None when one is allowed (no new text leaks)
    or the index can't be read (the ignore list is then what keeps denied
    files out)."""
    try:
        with sqlite3.connect(f"file:{_qmd_db_file()}?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT d.collection, d.path FROM documents d JOIN content c ON c.hash = d.hash "
                "WHERE d.active = 1 AND trim(c.doc, char(32, 9, 10, 13)) = ?", (output,)).fetchall()
    except sqlite3.Error:
        return None
    roots = _qmd_collection_roots()
    refusals = [qmd_uri_refusal(f"{collection}/{path}", roots) for collection, path in rows]
    if refusals and all(refusals):
        return refusals[0]
    return None


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
    round-robin, one block per file, conversation logs last. Hits that
    read_refusal/qmd_uri_refusal keep from agents are dropped."""
    alternatives = [a.strip() for a in query.split("|") if a.strip()] or [query.strip()]
    per_alt = []
    for alt in alternatives:
        blocks = allowed_qmd_blocks(_qmd_blocks(qmd_query(alt)))
        words = alt.split()
        if not blocks and len(words) > 1:
            for word in words:
                if len(word) >= 3:
                    blocks += allowed_qmd_blocks(_qmd_blocks(qmd_query(word, max_results=3)))
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
    the real on-disk path/casing. Denied files (read_refusal) come back as a
    "refused: ..." message the model can relay."""
    # Context-directory paths must work even before QMD indexes a note, and
    # must reflect current edits. Keep URI/search-result resolution with QMD.
    if Path(path_or_uri).is_absolute():
        try:
            return read_text_anywhere_in_vault(Path(path_or_uri))
        except VaultReadRefused as exc:
            return str(exc)
        except (OSError, ValueError) as exc:
            return f"(could not read '{path_or_uri}': {exc})"
    refusal = qmd_uri_refusal(path_or_uri)
    if refusal:
        return refusal
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
        text = result.stdout.strip()
        return _qmd_get_refusal(text) or text
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
    if read_refusal(f):
        raise MainVaultError("that's OpenClave's own settings or state; nothing is written there")
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
