"""Agents messaging each other (owner request, 2026-10-31).

An agent sends another agent a message with the message_agent tool. It lands
in the recipient's own conversation for that sender, titled "From <sender>"
(created on first use, found again by its From-agent front matter), as a
user turn marked with who sent it. The recipient then answers there like any
chat, and the owner can read and join the thread. The sender doesn't get the reply
back automatically; the recipient can answer with message_agent, which lands
in the sender's "From <recipient>" conversation.

A message from an agent is never the owner's word: chat_service tells the recipient
so, and it can't approve Home Assistant actions (ha_guard). Two agents
replying to each other are capped per pair, so they can't loop.

The tool may run inside the per-call MCP server (a separate process), so
delivery only writes the durable request and kicks the service
(chat_jobs.kick); the service runs it.
"""
import json
import re
from datetime import datetime, timedelta, timezone

import vault
from agents import AGENTS
from config import CONFIG

FROM_KEY = "From-agent"
MAX_CHARS = 8000
# Per sender→recipient pair: at most this many messages in the window.
PAIR_LIMIT = 4
PAIR_WINDOW = timedelta(minutes=10)


def resolve(name_or_id: str, sender: str | None = None) -> str | None:
    """An agent id from its id or name (any case, spaces ignored). Clones in
    different claves share a name (claves.py): then the one in the sender's
    clave wins."""
    key = re.sub(r"\s+", "", (name_or_id or "").strip().lstrip("@")).lower()
    if not key:
        return None
    found = [a for a, cfg in AGENTS.items() if key in (a.lower(), re.sub(r"\s+", "", cfg.name).lower())]
    if len(found) > 1 and sender:
        import claves
        home = claves.clave_of(sender)
        same = [a for a in found if home and claves.clave_of(a) == home]
        if same:
            return same[0]
    exact = [a for a in found if a.lower() == key]
    return (exact or found or [None])[0]


def conversation_for(recipient: str, sender: str, *, create: bool = True) -> str | None:
    """The recipient's "From <sender>" conversation."""
    found = vault.find_conversation(recipient, FROM_KEY, sender)
    if found or not create:
        return found
    return vault.create_conversation(recipient, f"From {AGENTS[sender].name}", {FROM_KEY: sender})


def _recent_from(recipient: str, sender: str) -> int:
    import chat_jobs  # chat_jobs → chat_service → this module
    cutoff = datetime.now(timezone.utc) - PAIR_WINDOW
    count = 0
    for path in chat_jobs._requests_dir(recipient).glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("sender") == sender and datetime.fromisoformat(record["created_at"]) >= cutoff:
                count += 1
        except (OSError, ValueError, KeyError):
            continue
    return count


def send(sender: str, to: str, message: str) -> dict:
    """Deliver and queue the recipient's reply. Raises ValueError with a
    reason the sending agent can act on."""
    recipient = resolve(to, sender)
    if sender not in AGENTS:
        raise ValueError("unknown sender")
    if not recipient:
        raise ValueError(f"no agent called {to!r}; use a name from the agent roster")
    if recipient == sender:
        raise ValueError("that's you; message another agent")
    message = (message or "").strip()
    if not message:
        raise ValueError("message is empty")
    if len(message) > MAX_CHARS:
        raise ValueError(f"message is {len(message)} characters; keep it under {MAX_CHARS} (point to a vault note for more)")
    if _recent_from(recipient, sender) >= PAIR_LIMIT:
        raise ValueError(f"you've sent {AGENTS[recipient].name} {PAIR_LIMIT} messages in the last "
                         f"{int(PAIR_WINDOW.total_seconds() // 60)} minutes; wait for a reply or ask {CONFIG.owner_name or 'the user'}")
    import chat_jobs
    conversation_id = conversation_for(recipient, sender)
    request_id = f"msg-{datetime.now():%Y%m%d%H%M%S}-{vault.new_conversation_id()[-8:]}"
    chat_jobs.submit(recipient, request_id, conversation_id, message, extra={"sender": sender}, schedule=False)
    chat_jobs.kick(recipient)
    return {"recipient": recipient, "conversation_id": conversation_id, "request_id": request_id}


def tool_reply(sender: str, to: str, message: str) -> str:
    try:
        result = send(sender, to, message)
    except ValueError as exc:
        return f"refused: {exc}"
    name = AGENTS[result["recipient"]].name
    return (f"Delivered to {name}, in their 'From {AGENTS[sender].name}' conversation. {name} will answer "
            f"there, where {CONFIG.owner_name or 'the user'} can read it; you won't see the answer unless they message you back.")


def threads(limit: int = 30) -> list[dict]:
    """Every agent-to-agent conversation, latest activity first."""
    out = []
    for recipient in AGENTS:
        for convo in vault.list_conversations(recipient):
            sender = convo.get("from_agent")
            if not sender:
                continue
            out.append({
                "agent_id": recipient, "agent_name": AGENTS[recipient].name,
                "from_agent": sender, "from_name": AGENTS[sender].name if sender in AGENTS else sender,
                "conversation_id": convo["id"], "title": convo["title"],
                "updated_ms": convo["updated_ms"], "last_is_reply": convo["last_is_reply"],
            })
    out.sort(key=lambda t: t["updated_ms"], reverse=True)
    return out[:limit]


def prompt_line(agent_id: str) -> str:
    if len(AGENTS) < 2:
        return ""
    return ("To ask another agent something or hand them data (a purchase to log, a finding in their domain), "
            "use message_agent: it lands in their 'From <you>' conversation and they act on it there. "
            "Messages from other agents arrive the same way, marked with the sender.\n")
