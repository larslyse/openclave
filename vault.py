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

_qmd_last_refresh: float = 0.0

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
from vault_identity import (  # noqa: F401
    IDENTITY_CHAR_LIMITS, IdentityLimitReached, _FEEDBACK_SECTION, _FOUNDATIONS_TABLE_HEADER,
    _replace_body, _send_identity_proposal, add_feedback, add_foundation, enforce_identity_limits,
    foundations_file, identity_limit_item_ids, identity_snapshot, read_foundations,
    read_identity_body, set_identity_body_by_owner, update_mission, update_persona,
)
from vault_files import (  # noqa: F401
    MAX_WORKSPACE_FILE_BYTES, _PROTECTED_WORKSPACE_FILES, _WORKSPACE_EXTENSIONS,
    _WORKSPACE_SKIP_DIRS, _unprotected_workspace_file, create_workspace_file, delete_workspace_file,
    handover_file, is_protected_workspace_file, list_workspace_files, move_workspace_file,
    read_dashboard, read_workspace_file, rename_workspace_file, workspace_file, write_dashboard,
    write_workspace_file,
)
from vault_search import (  # noqa: F401
    SEARCH_MAX_HITS, _QMD_REFRESH_MIN_INTERVAL_S, _denied_index_dirs, _glob_escape, _hit_file,
    _qmd_apply_ignores, _qmd_blocks, _qmd_collection_roots, _qmd_config_file, _qmd_db_file,
    _qmd_ensure_collection, _qmd_get_refusal, _qmd_ignore_patterns, _qmd_parts, _qmd_refresh_index,
    _qmd_segment, allowed_qmd_blocks, qmd_available, qmd_get, qmd_query, qmd_uri_refusal,
    search_language_hint, search_vault,
)
# @@END-REEXPORTS@@


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
