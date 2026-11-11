"""The maintainer inbox: one JSON file per flagged item in the maintainer's
folder. Part of vault.py's facade: import vault, not this module."""

import json
from datetime import datetime
from pathlib import Path

import vault
from agents import AGENTS
from vault_paths import _write_text_unlocked, file_lock, write_text
from vault_conversations import _CONVERSATION_ID_RE, new_conversation_id


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
    return vault.agent_root(maintainer) / "inbox" if maintainer else None


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
