"""Hand a conversation over to another agent, with its related files.

The owner (2026-09-29): a handover is the conversation itself plus any files that
belong to it, e.g. Rick's purchase log going to Stash. The conversation moves
as it is (vault.move_conversation keeps its history, photos and archives and
records where it came from in Prior-agents); the ticked files move to the same
path in the new agent's folder, so references in the chat stay valid. The new
agent then "replies" with a fixed "Handover registered" note that says where
it came from and which files came with it. No model call: it's instant and
nothing is rewritten behind the owner's back.

Started from `/handover <agent>` in any chat or the Hand over button in a
conversation list; see Build Docs/Chat commands.md in the vault.
"""
import re
from datetime import datetime

import chat_jobs
import vault
from agents import AGENTS

# Stem words shorter than this are too common to count as a mention ("log").
_MIN_WORD = 4
# Service records, not documents: maintainer inbox items.
_NEVER_MOVE_DIRS = {"inbox"}


def _movable(rel_path: str) -> bool:
    return rel_path.split("/", 1)[0] not in _NEVER_MOVE_DIRS


def _mentioned(rel_path: str, text: str) -> bool:
    """Whether the conversation names this file: its path or file name, or
    every longer word of its name (so "purchases.md" matches "my purchase log")."""
    rel = rel_path.lower()
    name = rel.rsplit("/", 1)[-1]
    if rel in text or name in text:
        return True
    # A plural file name still matches the singular in the chat ("purchases" ~ "purchase").
    words = [w[:-1] if w.endswith("s") else w
             for w in re.split(r"[^a-z0-9æøå]+", name.rsplit(".", 1)[0]) if len(w) >= _MIN_WORD]
    return bool(words) and all(w in text for w in words)


def related_files(agent_id: str, conversation_id: str) -> list[dict] | None:
    """Every file this agent could hand over, related ones first and marked
    suggested. None if the conversation doesn't exist. Core files (plan,
    backlog, mission, ...) never move, so they aren't offered."""
    f = vault.conversation_file(agent_id, conversation_id)
    if not f.exists():
        return None
    text = f.read_text(encoding="utf-8").lower()
    out = [{"path": e["path"], "suggested": _mentioned(e["path"], text)}
           for e in vault.list_workspace_files(agent_id) if not e["protected"] and _movable(e["path"])]
    out.sort(key=lambda e: not e["suggested"])  # stable: keeps list order within each group
    return out


def handover(source_agent: str, target_agent: str, conversation_id: str, files: list[str]) -> dict | None:
    """Move the conversation and the given files; returns what happened, or
    None if the conversation doesn't exist. Every file is checked before
    anything moves, so a name clash stops the whole handover up front."""
    if source_agent not in AGENTS or target_agent not in AGENTS or source_agent == target_agent:
        raise ValueError("choose a different valid target agent")
    target_name = AGENTS[target_agent].name
    files = list(dict.fromkeys(files))
    for rel in files:
        if not _movable(rel):
            raise ValueError(f"{rel} is a service record and can't be handed over")
        src = vault.handover_file(source_agent, rel)
        if not src.is_file():
            raise FileNotFoundError(f"{rel} not found")
        if vault.handover_file(target_agent, rel).exists():
            raise FileExistsError(f"{target_name} already has a file at {rel}")

    if not chat_jobs.move_conversation(source_agent, target_agent, conversation_id):
        return None
    moved, failed = [], []
    for rel in files:
        try:
            vault.move_workspace_file(source_agent, target_agent, rel)
            moved.append(rel)
        except (OSError, ValueError) as exc:
            failed.append({"path": rel, "error": str(exc)})

    source_name = AGENTS[source_agent].name
    lines = [f"Handover registered. This conversation came from {source_name} on {datetime.now():%Y-%m-%d}."]
    if moved:
        lines.append("Files that came with it, now in my folder: " + ", ".join(f"`{p}`" for p in moved) + ".")
    if failed:
        lines.append("Still with " + source_name + " (couldn't move): " + ", ".join(f"`{f['path']}`" for f in failed) + ".")
    vault.append_turn(target_agent, conversation_id, "assistant", "\n\n".join(lines))
    return {"conversation_id": conversation_id, "source_agent": source_agent, "target_agent": target_agent,
            "moved": moved, "failed": failed}
