"""Durable mobile chat request records with a small in-process worker pool."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time

import chat_service
import vault
import web_push
from agents import AGENTS
from config import CONFIG

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="openclave-chat")
_SUBMIT_LOCK = threading.Lock()
_SCHEDULED: set[str] = set()
_CANCELLED: set[str] = set()  # running requests whose provider call was stopped
# Recently finished replies, newest last, for Autojump (GET /api/replies).
# In memory only: a page that missed some across a restart just doesn't jump.
_RECENT_REPLIES: list[dict] = []
_RECENT_MAX = 50
_RECENT_LOCK = threading.Lock()
KICK_POLL_S = 2
# request_id -> last time a visible page polled it. A reply someone is
# already looking at gets no push (chat_core polls every 2s while open).
_WATCHED: dict[str, float] = {}
WATCH_WINDOW_S = 6


class RequestConflictError(RuntimeError):
    pass


class AgentBusyError(RuntimeError):
    pass


def _requests_dir(agent_id: str) -> Path:
    return vault.conversations_dir(agent_id) / "requests"


def _request_file(agent_id: str, request_id: str) -> Path:
    if not _REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError("invalid request id")
    return _requests_dir(agent_id) / f"{request_id}.json"


def _fingerprint(conversation_id: str | None, message: str, attachments: list[str]) -> str:
    raw = json.dumps([conversation_id, message, attachments], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _public(record: dict) -> dict:
    return {k: record.get(k) for k in ("request_id", "conversation_id", "status", "error", "created_at")}


def get(agent_id: str, request_id: str, *, watching: bool = False) -> dict | None:
    path = _request_file(agent_id, request_id)
    if watching:
        _WATCHED[request_id] = time.monotonic()
    if not path.exists():
        return None
    with vault.file_lock(agent_id, path) as scoped:
        return _public(json.loads(scoped.read_text(encoding="utf-8")))


def _load_full(agent_id: str, request_id: str) -> dict:
    path = _request_file(agent_id, request_id)
    with vault.file_lock(agent_id, path) as scoped:
        return json.loads(scoped.read_text(encoding="utf-8"))


def _save(agent_id: str, record: dict) -> None:
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    vault.write_text(agent_id, _request_file(agent_id, record["request_id"]), json.dumps(record, indent=2) + "\n")


def delete_conversation(agent_id: str, conversation_id: str) -> bool:
    # Serialize against submissions and active text/voice writes so a worker
    # cannot recreate a deleted conversation or lose its uploaded photos.
    with _SUBMIT_LOCK, chat_service.agent_execution(agent_id):
        for path in _requests_dir(agent_id).glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("conversation_id") == conversation_id and record.get("status") in {"queued", "running"}:
                raise AgentBusyError("Wait for this conversation's queued messages to finish before deleting it.")
        return vault.delete_conversation(agent_id, conversation_id)


def archive_conversation(agent_id: str, conversation_id: str) -> bool:
    """Same guard as delete: not while its messages are queued or running."""
    with _SUBMIT_LOCK, chat_service.agent_execution(agent_id):
        for path in _requests_dir(agent_id).glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("conversation_id") == conversation_id and record.get("status") in {"queued", "running"}:
                raise AgentBusyError("Wait for this conversation's queued messages to finish before archiving it.")
        done = vault.archive_conversation(agent_id, conversation_id)
    if done:
        import recaps
        recaps.schedule(agent_id, conversation_id)  # the Recaps tab's card, in the background
    return done


def while_idle(agent_id: str, conversation_id: str, fn, what: str = "changing it"):
    """Run fn() with the same guard as archive: not while the chat has work queued or running."""
    with _SUBMIT_LOCK, chat_service.agent_execution(agent_id):
        for path in _requests_dir(agent_id).glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("conversation_id") == conversation_id and record.get("status") in {"queued", "running"}:
                raise AgentBusyError(f"Wait for this conversation's queued messages to finish before {what}.")
        return fn()


def move_conversation(source_agent: str, target_agent: str, conversation_id: str) -> bool:
    if source_agent == target_agent or target_agent not in AGENTS:
        raise ValueError("choose a different valid target agent")
    # Agent locks also block concurrent voice calls and direct desktop chats.
    first, second = sorted((source_agent, target_agent))
    with _SUBMIT_LOCK, chat_service.agent_execution(first), chat_service.agent_execution(second):
        for agent_id in (source_agent, target_agent):
            for path in _requests_dir(agent_id).glob("*.json"):
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("conversation_id") == conversation_id and record.get("status") in {"queued", "running"}:
                    raise AgentBusyError("Wait for queued messages to finish before moving this conversation.")
        return vault.move_conversation(source_agent, target_agent, conversation_id)


def submit(
    agent_id: str, request_id: str, conversation_id: str | None, message: str,
    attachments: list[str] | None = None, *, extra: dict | None = None, schedule: bool = True,
) -> tuple[dict, bool]:
    """extra: fields kept on the record, e.g. sender (another agent's id) and
    context_note (model-only background); room ones come back in on_done.
    schedule=False only writes the record: for callers outside the service
    process (the per-call MCP server), which then kick() the service."""
    attachments = attachments or []
    if not message.strip() and not attachments:
        raise ValueError("message cannot be empty")
    path = _request_file(agent_id, request_id)
    fingerprint = _fingerprint(conversation_id, message, attachments)

    with _SUBMIT_LOCK:
        if path.exists():
            record = _load_full(agent_id, request_id)
            if record.get("fingerprint") != fingerprint:
                raise RequestConflictError("request id was already used with different input")
            return _public(record), False

        assigned_id = conversation_id or vault.new_conversation_id()
        vault.conversation_file(agent_id, assigned_id)
        vault.restore_if_archived(agent_id, assigned_id)  # chatting again un-archives it
        record = {
            "request_id": request_id,
            "conversation_id": assigned_id,
            "message": message,
            "attachments": attachments,
            "fingerprint": fingerprint,
            "status": "queued",
            "error": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            **(extra or {}),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({**record, "updated_at": record["created_at"]}, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        if not schedule:
            return _public(record), True
        start = agent_id not in _SCHEDULED
        _SCHEDULED.add(agent_id)
    if start:
        _EXECUTOR.submit(_drain, agent_id)
    return _public(record), True


def _kick_dir() -> Path:
    path = CONFIG.state_path("chat-kick")
    path.mkdir(exist_ok=True)
    return path


def kick(agent_id: str) -> None:
    """Ask the service to run an agent's queued requests (written with
    schedule=False). Safe from any process: it's a marker file."""
    (_kick_dir() / agent_id).touch()


def kick_once() -> None:
    """Hands each kicked agent's queue to the chat executor."""
    for marker in _kick_dir().iterdir():
        agent_id = marker.name
        marker.unlink(missing_ok=True)
        if agent_id not in AGENTS:
            continue
        with _SUBMIT_LOCK:
            start = agent_id not in _SCHEDULED
            _SCHEDULED.add(agent_id)
        if start:
            _EXECUTOR.submit(_drain, agent_id)


def start_kicker() -> None:
    """Registers the kick poll with the scheduler: every KICK_POLL_S seconds,
    inline (it only lists a folder). It is part of normal chat handling, so it
    runs even with background jobs switched off."""
    import scheduler
    scheduler.register("chat-kick", "Run messages agents queued", kick_once,
                       every=KICK_POLL_S, delay=KICK_POLL_S, background=False)


def _drain(agent_id: str) -> None:
    """One worker per agent processes durable requests in acceptance order."""
    while True:
        with _SUBMIT_LOCK:
            queued = []
            for path in _requests_dir(agent_id).glob("*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if record.get("status") == "queued":
                    queued.append(record)
            if not queued:
                _SCHEDULED.discard(agent_id)
                return
            record = min(queued, key=lambda r: (r["created_at"], r["request_id"]))
        _run(agent_id, record["request_id"])


def cancel(agent_id: str, request_id: str) -> dict | None:
    """Queued: never runs. Running: the CLI process is stopped, or the API
    stream at its next chunk, and the request ends as cancelled. Nothing
    running to stop: the caller gets stoppable=False."""
    path = _request_file(agent_id, request_id)
    if not path.exists():
        return None
    with _SUBMIT_LOCK:
        record = _load_full(agent_id, request_id)
        if record["status"] == "queued":
            record["status"] = "cancelled"
            record["error"] = "Cancelled before it started."
            _save(agent_id, record)
            return {**_public(record), "stoppable": True}
    if record["status"] != "running":
        return {**_public(record), "stoppable": False}
    import llm_client
    _CANCELLED.add(request_id)
    if not llm_client.cancel_running(agent_id):
        _CANCELLED.discard(request_id)
        return {**_public(record), "stoppable": False}
    return {**_public(record), "stoppable": True}


def _run(agent_id: str, request_id: str) -> None:
    record = _load_full(agent_id, request_id)
    if record.get("status") != "queued":  # cancelled while waiting its turn
        return
    try:
        with chat_service.agent_execution(agent_id, wait=True):
            record["status"] = "running"
            _save(agent_id, record)
            # Only another agent's or the group chat's requests carry these.
            extra = {k: record[k] for k in ("sender", "context_note") if record.get(k)}
            reply = chat_service.run_chat_locked(
                agent_id, record["conversation_id"], record["message"], record.get("attachments", []), **extra
            )
            record["status"] = "completed"
            record["error"] = None
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = str(exc)[:500]
        reply = None
    if request_id in _CANCELLED:
        _CANCELLED.discard(request_id)
        if record["status"] == "failed":
            record["status"] = "cancelled"
            record["error"] = "Stopped."
            # Keep the transcript a clean user/assistant exchange.
            try:
                vault.append_turn(agent_id, record["conversation_id"], "assistant", "(Stopped by the user before replying.)")
            except Exception:
                pass
    _save(agent_id, record)
    if record["status"] in ("completed", "failed"):
        _push_reply(agent_id, record, reply)
    if record["status"] == "completed":
        _remember_reply(agent_id, record["conversation_id"])
    if record.get("room"):
        import room
        try:
            room.on_done(agent_id, record, reply)
        except Exception as exc:  # noqa: BLE001 -- the agent's own chat already has the reply
            print(f"group chat update for {agent_id} failed: {exc}")


def _remember_reply(agent_id: str, conversation_id: str) -> None:
    with _RECENT_LOCK:
        last = _RECENT_REPLIES[-1]["finished_ms"] if _RECENT_REPLIES else 0
        # Strictly increasing, so "after=<ms>" never skips a same-ms reply.
        finished = max(int(time.time() * 1000), last + 1)
        _RECENT_REPLIES.append({"agent_id": agent_id, "conversation_id": conversation_id, "finished_ms": finished})
        del _RECENT_REPLIES[:-_RECENT_MAX]


def recent_replies(after_ms: int) -> dict:
    with _RECENT_LOCK:
        replies = [dict(r) for r in _RECENT_REPLIES if r["finished_ms"] > after_ms]
    for r in replies:
        agent = AGENTS.get(r["agent_id"])
        r["agent_name"] = agent.name if agent else r["agent_id"]
    now = int(time.time() * 1000)
    return {"now_ms": max([now] + [r["finished_ms"] for r in replies]), "replies": replies}


def _snippet(text: str | None, limit: int = 140) -> str:
    text = text if isinstance(text, str) else ""
    plain = re.sub(r"[*_`#>|\[\]]+", "", re.sub(r"\s+", " ", text)).strip()
    return plain if len(plain) <= limit else plain[:limit - 1].rstrip() + "…"


def _push_reply(agent_id: str, record: dict, reply: str | None) -> None:
    """Web Push when a reply is ready (Rick's backlog, 2026-10-02): until now
    only the open chat noticed a finished reply, so a notification arrived
    when the user opened that agent, not when it finished. Skipped when a visible
    page is watching this request. Sent off-thread; never raises."""
    seen = _WATCHED.pop(record["request_id"], None)
    if seen is not None and time.monotonic() - seen < WATCH_WINDOW_S:
        return
    if record.get("room"):
        import room
        if room.watched():
            return
    agent = AGENTS.get(agent_id)
    name = agent.name if agent else agent_id
    if record["status"] == "completed":
        title, body = f"{name} replied", _snippet(reply) or "Reply ready."
    else:
        title, body = f"{name} couldn't reply", _snippet(record.get("error") or "", 200) or "The request failed."
    url = f"/mobile/#/agents/{agent_id}/conversations/{record['conversation_id']}"

    def send():
        try:
            web_push.send_all(title, body, url=url, tag=f"openclave-reply-{agent_id}")
        except Exception as exc:
            print(f"reply push for {agent_id} failed: {exc}")
    threading.Thread(target=send, name="openclave-reply-push", daemon=True).start()


def recover() -> None:
    """Resume never-started records; mark uncertain running work interrupted."""
    for agent_id in AGENTS:
        directory = _requests_dir(agent_id)
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if record.get("status") == "running":
                record["status"] = "interrupted"
                record["error"] = "Service restarted during processing; actions may have completed. Check before retrying."
                _save(agent_id, record)

        with _SUBMIT_LOCK:
            schedule = agent_id not in _SCHEDULED
            _SCHEDULED.add(agent_id)
        if schedule:
            _EXECUTOR.submit(_drain, agent_id)
