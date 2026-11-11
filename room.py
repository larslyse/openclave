"""The group chat: the owner and every agent in one room (2026-10-31).

The desktop's chat view shows it to the right of the agent list. Who answers
a message:
- the agents it @mentions (by name or id; @all / @everyone means all), else
- the agents who answered the owner's previous message (the thread carries on).
With neither, nobody is asked and the page says to mention someone.

An agent answers through its normal chat (tools, memory, persona) in its own
"Group chat" conversation, with the recent room messages as background; its
reply is copied into the room. A reply that @mentions another agent asks that
agent too, at most MAX_HOPS deep from the owner's message, so agents can talk to
each other without running away.

The room is a JSON-lines file in OpenClave's state. Everything here runs in
the service process (chat_jobs calls on_done from its worker).

Channels (channels.py, 2026-11-05): rooms the owner makes, with chosen members.

Claves (claves.py, 2026-11-04): each clave has its own room with only its
members; "everyone" is the original room with every agent. An agent answers
a clave's room in its own "Group chat · <clave>" conversation, filed in a chat
folder named after the clave.
"""
import json
import re
import threading
import time
import uuid

import channels
import chat_jobs
import claves
from agents import AGENTS
from config import CONFIG
from state_io import load_json, save_json

ROOM_FILE = CONFIG.state_path("room.jsonl")
CONVERSATIONS_FILE = CONFIG.state_path("room_conversations.json")
ROOM_KEY = "Room"
ROOM_TITLE = "Group chat"
CONTEXT_MESSAGES = 20
MAX_HOPS = 3
MAX_CHARS = 8000
_LOCK = threading.Lock()
_MENTION_RE = re.compile(r"(?<![\w@])@([\w-]+)")
_EVERYONE = {"all", "everyone", "alle"}


def _members(room_id: str) -> list[str]:
    return channels.members(room_id) if channels.is_channel(room_id) else claves.members(room_id)


def _room_name(room_id: str) -> str:
    return f"#{channels.get(room_id)['name']}" if channels.is_channel(room_id) else claves.name_of(room_id)


def _file(clave: str):
    if clave == claves.EVERYONE:
        return ROOM_FILE
    return ROOM_FILE.with_name(f"room-{clave}.jsonl")


def _read(clave: str = claves.EVERYONE) -> list[dict]:
    try:
        lines = _file(clave).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _append(message: dict, clave: str = claves.EVERYONE) -> dict:
    with _LOCK:
        last = _read(clave)[-1:] or [{"seq": 0}]
        message = {"seq": last[0]["seq"] + 1, "ts_ms": int(time.time() * 1000), **message}
        with _file(clave).open("a", encoding="utf-8") as out:
            out.write(json.dumps(message, ensure_ascii=False) + "\n")
    return message


def mentions(text: str, *, exclude: str | None = None, clave: str = claves.EVERYONE) -> list[str]:
    """Agent ids @mentioned in text, in order, among the room's members.
    Names with spaces match without them (@MrFix for "Mr Fix")."""
    # In a channel anyone can be tagged in (post() then adds them).
    present = list(AGENTS) if channels.is_channel(clave) else claves.members(clave)
    keys = {}
    for agent_id in present:
        keys[agent_id.lower()] = agent_id
        keys[re.sub(r"\s+", "", AGENTS[agent_id].name).lower()] = agent_id
    found = []
    for word in _MENTION_RE.findall(text or ""):
        word = word.lower()
        ids = list(_members(clave)) if word in _EVERYONE else [keys[word]] if word in keys else []
        for agent_id in ids:
            if agent_id != exclude and agent_id not in found:
                found.append(agent_id)
    return found


def _conversation(agent_id: str, clave: str = claves.EVERYONE) -> str:
    """The agent's own "Group chat" conversation, where it answers the room."""
    import vault
    known = load_json(CONVERSATIONS_FILE, {})
    if not isinstance(known, dict):
        known = {}
    key = agent_id if clave == claves.EVERYONE else f"{clave}:{agent_id}"
    conversation_id = known.get(key)
    try:
        if conversation_id and vault.conversation_file(agent_id, conversation_id).exists():
            return conversation_id
    except ValueError:
        pass
    if clave == claves.EVERYONE:
        conversation_id = vault.find_conversation(agent_id, ROOM_KEY, "true") or \
            vault.create_conversation(agent_id, ROOM_TITLE, {ROOM_KEY: "true"})
    elif channels.is_channel(clave):
        conversation_id = vault.find_conversation(agent_id, ROOM_KEY, clave) or \
            vault.create_conversation(agent_id, f"{ROOM_TITLE} · {_room_name(clave)}",
                                      {ROOM_KEY: clave, "Folder": channels.FOLDER})
    else:
        name = claves.name_of(clave)
        conversation_id = vault.find_conversation(agent_id, ROOM_KEY, clave) or \
            vault.create_conversation(agent_id, f"{ROOM_TITLE} · {name}", {ROOM_KEY: clave, "Folder": name})
    known[key] = conversation_id
    save_json(CONVERSATIONS_FILE, known)
    return conversation_id


def _speaker(message: dict) -> str:
    sender = message.get("from")
    if sender == "user":
        return CONFIG.owner_name or "User"
    return AGENTS[sender].name if sender in AGENTS else str(sender)


def _context(agent_id: str, upto_seq: int, clave: str = claves.EVERYONE) -> str:
    recent = [m for m in _read(clave) if m["seq"] < upto_seq][-CONTEXT_MESSAGES:]
    lines = [f"{_speaker(m)}: {m['text']}" for m in recent]
    roster = ", ".join(AGENTS[a].name for a in _members(clave))
    who = f"every agent ({roster})" if clave == claves.EVERYONE else f"these agents ({roster})"
    where = (f"This is the {_room_name(clave)} channel." if channels.is_channel(clave)
             else claves.clave_context(clave))
    return ("# Group chat (background)\n"
            f"You are {AGENTS[agent_id].name}, in OpenClave's group chat with {CONFIG.owner_name or 'the user'} and "
            f"{who}. Everyone reads your reply. Keep it short and in your domain; "
            "@mention another agent by name only when you need them to answer.\n"
            + (where + "\n" if where else "")
            + ("Earlier messages:\n" + "\n".join(lines) if lines else "(No earlier messages.)"))


def _ask(agent_id: str, trigger: dict, clave: str = claves.EVERYONE) -> None:
    conversation_id = _conversation(agent_id, clave)
    sender = trigger["from"] if trigger["from"] != "user" else None
    request_id = f"room-{trigger['seq']}-{agent_id}-{uuid.uuid4().hex[:6]}"
    chat_jobs.submit(agent_id, request_id, conversation_id, trigger["text"], extra={
        "sender": sender, "context_note": _context(agent_id, trigger["seq"], clave),
        "room": {"seq": trigger["seq"], "hops": trigger.get("hops", 0), "clave": clave},
    })
    with _LOCK:
        _PENDING[request_id] = {"agent_id": agent_id, "seq": trigger["seq"], "clave": clave}


# request_id -> {agent_id, seq, clave}: room questions an agent is still answering.
_PENDING: dict[str, dict] = {}


def _responders(text: str, clave: str = claves.EVERYONE) -> list[str]:
    named = mentions(text, clave=clave)
    if named:
        return named
    # Carry on with whoever answered the owner's previous message.
    present = set(_members(clave))
    history = _read(clave)
    last_user = next((m for m in reversed(history) if m["from"] == "user"), None)
    if not last_user:
        return []
    return list(dict.fromkeys(m["from"] for m in history
                              if m.get("reply_to") == last_user["seq"] and m["from"] in present))


def _check(clave: str) -> str:
    clave = clave or claves.EVERYONE
    if channels.is_channel(clave):
        channels.get(clave)  # KeyError for an unknown channel
    elif clave != claves.EVERYONE:
        claves.get(clave)  # KeyError for an unknown clave
    return clave


def post(text: str, clave: str = claves.EVERYONE) -> dict:
    """The owner's message. Returns it with the agents asked to answer."""
    clave = _check(clave)
    text = (text or "").strip()
    if not text:
        raise ValueError("message cannot be empty")
    if len(text) > MAX_CHARS:
        raise ValueError(f"message is too long (max {MAX_CHARS} characters)")
    responders = _responders(text, clave)
    joined = channels.add_members(clave, responders) if channels.is_channel(clave) else []
    message = _append({"from": "user", "text": text, "hops": 0, "asked": responders,
                       **({"joined": joined} if joined else {})}, clave)
    for agent_id in responders:
        _ask(agent_id, message, clave)
    return message


def on_done(agent_id: str, record: dict, reply: str | None) -> None:
    """chat_jobs: an agent finished (or failed) answering the room."""
    meta = record.get("room") or {}
    clave = meta.get("clave") or claves.EVERYONE
    try:
        _check(clave)
    except KeyError:
        return  # the clave was archived while the agent was answering
    with _LOCK:
        _PENDING.pop(record["request_id"], None)
    if record.get("status") != "completed" or not reply:
        _append({"from": agent_id, "text": f"(Couldn't answer: {record.get('error') or 'no reply'})",
                 "reply_to": meta.get("seq"), "error": True, "hops": meta.get("hops", 0) + 1}, clave)
        return
    hops = meta.get("hops", 0) + 1
    asked = mentions(reply, exclude=agent_id, clave=clave) if hops < MAX_HOPS else []
    if channels.is_channel(clave):  # agents tagging an outsider don't add them: only the owner does
        asked = [a for a in asked if a in channels.members(clave)]
    message = _append({"from": agent_id, "text": reply.strip(), "reply_to": meta.get("seq"),
                       "conversation_id": record.get("conversation_id"), "hops": hops, "asked": asked}, clave)
    for other in asked:
        _ask(other, message, clave)


def pending(clave: str = claves.EVERYONE) -> list[dict]:
    """Agents still working on a room message. Drops requests that ended
    without on_done (a restart marks them interrupted)."""
    with _LOCK:
        items = list(_PENDING.items())
    out = []
    for request_id, item in items:
        if item.get("clave", claves.EVERYONE) != clave:
            continue
        status = (chat_jobs.get(item["agent_id"], request_id) or {}).get("status")
        if status in ("queued", "running"):
            out.append({"agent_id": item["agent_id"], "seq": item["seq"], "status": status})
        else:
            with _LOCK:
                _PENDING.pop(request_id, None)
    return out


_WATCHED_AT = [0.0]
WATCH_WINDOW_S = 8


def watched() -> bool:
    """Whether a page showed the room in the last few seconds (no push then)."""
    return time.monotonic() - _WATCHED_AT[0] < WATCH_WINDOW_S


def messages(after: int = 0, limit: int = 200, clave: str = claves.EVERYONE) -> dict:
    clave = _check(clave)
    _WATCHED_AT[0] = time.monotonic()
    items = [m for m in _read(clave) if m["seq"] > after][-limit:]
    for m in items:
        m["name"] = _speaker(m)
    return {"messages": items, "pending": pending(clave), "members": _members(clave)}
