"""Agent conversations: list, read, append, compact, move, archive and restore.
Part of vault.py's facade: import vault, not this module."""

import json
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import vault
from agents import AGENTS
from vault_paths import _write_keeping_mtime, _write_text_unlocked, conversations_dir, file_lock, render_frontmatter, split_frontmatter, write_text
from vault_attachments import attachments_dir, turn_attachments


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
