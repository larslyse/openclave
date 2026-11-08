"""The manager's proposals: what it wants to do, for the owner to approve (2026-11-03).

The manager (agents.manager_id) may suggest anything that would help. It
never acts on its own judgment outside its own folder: it files a proposal
with propose_to_owner, and the owner answers Yes / No / Discuss on the
manager's page. Three kinds:

  - vault_note: write something worth keeping into the main vault (append to
    an existing note, or a new note in an existing folder). Written by
    vault.write_main_vault_note, which refuses agent folders, confidential
    folders, new folders and anything but Markdown.
  - message_agent: send another agent a message (e.g. "your plan is a month
    stale"), delivered like message_agent from the manager.
  - task: anything else the manager wants to do itself; on Yes it gets a
    message in its Audit conversation to go ahead.
  - model (2026-11-04): add a new model to the pickers, or remove an old one.
    Filed by the audit itself (audit.model_proposals), not by the manager's
    tool; Yes calls model_updater.add_available / hide.

The weekly audit (audit.py) is where most proposals come from, but the
manager can propose at any time. Declined ones are remembered for a while so
they don't come back the next week.

Ops state, not agent knowledge: OPENCLAVE_HOME/state/manager_proposals.json.
"""
import json
import threading
import uuid
from datetime import datetime, timedelta

import agents
import vault
from agents import AGENTS
from config import CONFIG

STATE_FILE = CONFIG.state_path("manager_proposals.json")
KINDS = ("vault_note", "message_agent", "task", "model")
MODEL_ACTIONS = ("add", "hide")
AUDIT_CONVERSATION = "audit"
MAX_OPEN = 20
MAX_CONTENT = 8000
KEEP_DONE = 60          # answered proposals kept (for "don't propose this again")
_LOCK = threading.Lock()


def _load() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def _clean(text, limit: int) -> str:
    return str(text or "").strip()[:limit]


def add(kind: str, title: str, why: str, content: str, target: str | None = None,
        mode: str | None = None, agent: str | None = None, source: str | None = None,
        model_action: str | None = None, model_id: str | None = None) -> dict:
    """File one proposal. Raises ValueError with a reason the manager can act on."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    title, why, content = _clean(title, 120), _clean(why, 600), _clean(content, MAX_CONTENT)
    if not title or not content:
        raise ValueError("a proposal needs a title and its content")
    item = {"id": uuid.uuid4().hex[:8], "kind": kind, "title": title, "why": why, "content": content,
            "created": datetime.now().strftime("%Y-%m-%d %H:%M"), "status": "open"}
    if kind == "vault_note":
        mode = mode or "append"
        if mode not in ("append", "create"):
            raise ValueError("mode must be append or create")
        f = vault.main_vault_target(target or "")  # raises MainVaultError (a ValueError)
        if mode == "append" and not f.exists():
            raise ValueError(f"{target} doesn't exist; search for the right note, or use mode=create")
        if mode == "create" and f.exists():
            raise ValueError(f"{target} already exists; use mode=append")
        if mode == "create" and not f.parent.is_dir():
            raise ValueError("new notes go in existing folders; that folder doesn't exist")
        item.update(target=str(f.relative_to(vault.VAULT_ROOT)), mode=mode, source=_clean(source, 300))
    elif kind == "message_agent":
        import agent_messages
        recipient = agent_messages.resolve(agent or "")
        if not recipient:
            raise ValueError(f"no agent called {agent!r}")
        if recipient == agents.manager_id():
            raise ValueError("that's you")
        item.update(agent=recipient)
    elif kind == "model":
        if model_action not in MODEL_ACTIONS or not model_id:
            raise ValueError("a model proposal needs model_action (add or hide) and model_id")
        item.update(model_action=model_action, model_id=model_id)
    with _LOCK:
        state = _load()
        items = state.setdefault("items", [])
        open_items = [i for i in items if i["status"] == "open"]
        if len(open_items) >= MAX_OPEN:
            raise ValueError(f"{MAX_OPEN} proposals are already waiting; wait for answers before adding more")
        if any(i["title"].lower() == title.lower() for i in open_items):
            raise ValueError("a proposal with that title is already waiting")
        items.append(item)
        _save(state)
    return item


def tool_reply(manager: str, tool_input: dict) -> str:
    if not agents.has_role(manager, agents.MANAGER):
        return "refused: only the manager files proposals"
    if tool_input.get("kind") == "model":
        return "refused: model proposals are filed by the audit itself"
    try:
        item = add(tool_input.get("kind", ""), tool_input.get("title", ""), tool_input.get("why", ""),
                   tool_input.get("content", ""), tool_input.get("target"), tool_input.get("mode"),
                   tool_input.get("agent"), tool_input.get("source"))
    except ValueError as exc:
        return f"refused: {exc}"
    return (f"Proposal {item['id']} filed. {CONFIG.owner_name or 'The user'} sees it on your page "
            "and answers Yes / No / Discuss; nothing happens until then.")


def _public(item: dict) -> dict:
    out = dict(item)
    if item.get("agent"):
        out["agent_name"] = AGENTS[item["agent"]].name if item["agent"] in AGENTS else item["agent"]
    return out


def list_open() -> list[dict]:
    return [_public(i) for i in _load().get("items", []) if i["status"] == "open"]


def recent_answered(days: int = 60) -> list[dict]:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return [i for i in _load().get("items", []) if i["status"] != "open" and i.get("answered", "") >= cutoff]


def _finish(item_id: str, status: str, note: str | None = None, result: str | None = None) -> None:
    with _LOCK:
        state = _load()
        for i in state.get("items", []):
            if i["id"] == item_id:
                i.update(status=status, answered=datetime.now().strftime("%Y-%m-%d %H:%M"))
                if note:
                    i["note"] = _clean(note, 600)
                if result:
                    i["result"] = result
        done = [i for i in state.get("items", []) if i["status"] != "open"]
        drop = {i["id"] for i in done[:-KEEP_DONE]} if len(done) > KEEP_DONE else set()
        state["items"] = [i for i in state.get("items", []) if i["id"] not in drop]
        _save(state)


def _get_open(item_id: str) -> dict:
    found = next((i for i in _load().get("items", []) if i["id"] == item_id and i["status"] == "open"), None)
    if not found:
        raise LookupError("that proposal is no longer waiting")
    return found


def _tell_manager(text: str) -> None:
    """Queue a message to the manager in its Audit conversation."""
    import chat_jobs
    manager = agents.manager_id()
    if not manager:
        return
    vault.ensure_conversation(manager, AUDIT_CONVERSATION, "Audit")
    request_id = f"proposal-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
    chat_jobs.submit(manager, request_id, AUDIT_CONVERSATION, text, schedule=False)
    chat_jobs.kick(manager)


def approve(item_id: str, content: str | None = None) -> dict:
    """The owner said yes (optionally after editing the content)."""
    item = _get_open(item_id)
    content = _clean(content, MAX_CONTENT) if content is not None else item["content"]
    if not content:
        raise ValueError("nothing to write")
    if item["kind"] == "vault_note":
        written = vault.write_main_vault_note(item["target"], content, item["mode"], item["title"])
        result = f"wrote {written}"
    elif item["kind"] == "message_agent":
        import agent_messages
        manager = agents.manager_id()
        if not manager:
            raise ValueError("there's no manager to send it from")
        agent_messages.send(manager, item["agent"], content)
        result = f"sent to {AGENTS[item['agent']].name if item['agent'] in AGENTS else item['agent']}"
    elif item["kind"] == "model":
        import model_updater
        if item["model_action"] == "add":
            model_updater.add_available(item["model_id"])
            result = "added to the model pickers"
        else:
            model_updater.hide(item["model_id"], item["why"] or "old")
            result = "removed from the model pickers (Restore in the Control Panel brings it back)"
    else:
        _tell_manager(f"Approved: {item['title']}\n\n{content}\n\nGo ahead now, then report back here in a line or two.")
        result = "the manager is on it"
    _finish(item_id, "approved", result=result)
    return {"done": True, "result": result}


def decline(item_id: str, note: str | None = None) -> bool:
    _get_open(item_id)
    _finish(item_id, "declined", note=note)
    return True


def discuss(item_id: str) -> dict:
    """Where to talk it over: the manager's Audit conversation, with a draft."""
    item = _get_open(item_id)
    manager = agents.manager_id()
    return {"agent_id": manager, "conversation_id": AUDIT_CONVERSATION,
            "draft": f"About your proposal \"{item['title']}\" ({item['id']}): "}
