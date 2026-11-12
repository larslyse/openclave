"""Scheduled reminders: the first agent "action" (Rick's backlog, 2026-09-25).

An agent schedules a message for the user (schedule_reminder tool). When it's due,
the service posts it into that agent's "Reminders" conversation and pushes it
to the user's devices (web_push.py). Two kinds:
  - fixed (default): the text written when it was scheduled. Nothing runs at
    send time.
  - compose (added 2026-10-04): the stored text is an instruction ("check
    tomorrow's rain forecast; tell the user whether to water the tomatoes"). When
    it's due, the agent runs once in its Reminders conversation, looking
    things up only, and its reply is what the user gets. Look-up only is enforced
    where it matters: R2's Home Assistant connection refuses anything but
    reads in these runs (llm_client.READ_ONLY_RUNS). If the run fails, the user
    still gets the instruction, marked as not prepared.

Stored per agent in its own vault folder (reminders.json), so it's visible and
editable in the Files tab and backed up with the vault. Times are the user's local
time in the configured timezone, while the server itself may run on UTC.
"""
import json
import threading
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import vault
import config
from agents import AGENTS

TZ = ZoneInfo(config.CONFIG.timezone)
CONVERSATION_ID = "reminders"
REPEATS = ("daily", "weekly", "monthly")
MAX_ACTIVE = 50
MAX_MESSAGE_CHARS = 1000
HISTORY_KEEP = 50
_FMT = "%Y-%m-%dT%H:%M"


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)


def describe_now() -> str:
    return now_local().strftime("%A %Y-%m-%d %H:%M") + f" ({config.CONFIG.timezone})"


def _file(agent_id: str):
    return vault.agent_root(agent_id) / "reminders.json"


def _load(agent_id: str) -> dict:
    try:
        data = json.loads(_file(agent_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"reminders": [r for r in data.get("reminders", []) if isinstance(r, dict) and r.get("next_at")],
            "history": [h for h in data.get("history", []) if isinstance(h, dict)]}


def _save_unlocked(path, data: dict) -> None:
    vault._write_text_unlocked(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _parse_at(at: str) -> datetime:
    text = (at or "").strip().replace(" ", "T")
    for fmt in (_FMT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(hour=9) if fmt == "%Y-%m-%d" else parsed.replace(second=0)
        except ValueError:
            continue
    raise ValueError(f"can't read time {at!r}: use YYYY-MM-DD HH:MM, local time ({config.CONFIG.timezone})")


def _advance(when: datetime, repeat: str) -> datetime:
    if repeat == "daily":
        return when + timedelta(days=1)
    if repeat == "weekly":
        return when + timedelta(weeks=1)
    # monthly: same day next month, or the month's last day if it's shorter
    year, month = (when.year + 1, 1) if when.month == 12 else (when.year, when.month + 1)
    for day in (when.day, 30, 29, 28):
        try:
            return when.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def _public(r: dict) -> dict:
    return {**{k: r.get(k) for k in ("id", "message", "next_at", "repeat", "created")}, "compose": bool(r.get("compose"))}


def add(agent_id: str, message: str, at: str, repeat: str | None = None, compose: bool = False) -> dict:
    message = (message or "").strip()
    if not message:
        raise ValueError("message can't be empty")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(f"message is {len(message)} characters; keep it under {MAX_MESSAGE_CHARS}")
    repeat = (repeat or "").strip().lower() or None
    if repeat is not None and repeat not in REPEATS:
        raise ValueError(f"repeat must be one of {', '.join(REPEATS)} (or left out for once)")
    when = _parse_at(at)
    now = now_local()
    if when <= now:
        raise ValueError(f"{when.strftime(_FMT)} is not in the future (now is {describe_now()})")
    if when > now + timedelta(days=366 * 2):
        raise ValueError("that's more than two years away")
    reminder = {"id": uuid.uuid4().hex[:8], "message": message, "next_at": when.strftime(_FMT),
                "repeat": repeat, "created": now.strftime(_FMT)}
    if compose:
        reminder["compose"] = True
    path = _file(agent_id)
    with vault.file_lock(agent_id, path) as scoped:
        data = _load(agent_id)
        if len(data["reminders"]) >= MAX_ACTIVE:
            raise ValueError(f"already {MAX_ACTIVE} reminders scheduled; cancel some first")
        data["reminders"].append(reminder)
        _save_unlocked(scoped, data)
    return _public(reminder)


def list_for(agent_id: str) -> list[dict]:
    return sorted((_public(r) for r in _load(agent_id)["reminders"]), key=lambda r: r["next_at"])


def history_for(agent_id: str) -> list[dict]:
    return list(reversed(_load(agent_id)["history"]))


def cancel(agent_id: str, reminder_id: str) -> bool:
    path = _file(agent_id)
    if not path.exists():
        return False
    with vault.file_lock(agent_id, path) as scoped:
        data = _load(agent_id)
        kept = [r for r in data["reminders"] if r.get("id") != reminder_id]
        if len(kept) == len(data["reminders"]):
            return False
        data["reminders"] = kept
        _save_unlocked(scoped, data)
    return True


def take_due(agent_id: str, now: datetime | None = None) -> list[dict]:
    """Remove due reminders (or move repeating ones to their next time),
    record them in history, and return them for delivery. Reminders missed
    while the service was down go out once, late; missed repeats are skipped."""
    path = _file(agent_id)
    if not path.exists():
        return []
    now = now or now_local()
    stamp = now.strftime(_FMT)
    with vault.file_lock(agent_id, path) as scoped:
        data = _load(agent_id)
        due = [r for r in data["reminders"] if str(r["next_at"]) <= stamp]
        if not due:
            return []
        kept = []
        for r in data["reminders"]:
            if r not in due:
                kept.append(r)
                continue
            if r.get("repeat") in REPEATS:
                try:
                    nxt = _parse_at(r["next_at"])
                except ValueError:
                    continue  # hand-edited into something unreadable: drop it rather than loop
                while nxt <= now:
                    nxt = _advance(nxt, r["repeat"])
                kept.append({**r, "next_at": nxt.strftime(_FMT)})
        data["reminders"] = kept
        data["history"] = (data["history"] + [
            {"id": r.get("id"), "message": r.get("message", ""), "scheduled_for": r["next_at"], "sent_at": stamp,
             **({"compose": True} if r.get("compose") else {})}
            for r in due
        ])[-HISTORY_KEEP:]
        _save_unlocked(scoped, data)
    return due


def _push(agent_id: str, reminder: dict, text: str) -> int:
    import web_push
    return web_push.send_all(
        title=AGENTS[agent_id].name, body=text,
        url=f"/mobile/#/agents/{agent_id}/conversations/{CONVERSATION_ID}",
        tag=f"openclave-reminder-{agent_id}-{reminder.get('id')}",
    )


def deliver(agent_id: str, reminder: dict) -> int:
    """Post into the agent's Reminders conversation, then push. Returns how
    many devices the push reached."""
    if reminder.get("compose"):
        return compose_and_deliver(agent_id, reminder)
    message = str(reminder.get("message", "")).strip()
    vault.append_turn(agent_id, CONVERSATION_ID, "assistant", message, first_message="Reminders")
    return _push(agent_id, reminder, message)


COMPOSE_RULES = ("This is a reminder you scheduled earlier, running now; the user isn't waiting in the chat. "
                 "Follow the instruction, using your tools only to look things up (no changes to files, "
                 "settings or devices), and reply with just the message for the user, under 1000 characters.")


def compose_and_deliver(agent_id: str, reminder: dict) -> int:
    """Run the agent once on the stored instruction; its reply is the reminder.
    Waits its turn behind any chat the agent is busy with."""
    import chat_service
    import llm_client
    instruction = str(reminder.get("message", "")).strip()
    if not vault.conversation_file(agent_id, CONVERSATION_ID).exists():
        vault.append_turn(agent_id, CONVERSATION_ID, "assistant", "Reminders you scheduled arrive here.", first_message="Reminders")
    try:
        with chat_service.agent_execution(agent_id, wait=True):
            llm_client.READ_ONLY_RUNS.add(agent_id)
            try:
                text = chat_service.run_chat_locked(
                    agent_id, CONVERSATION_ID, f"(Scheduled reminder) {instruction}\n\n{COMPOSE_RULES}")
            finally:
                llm_client.READ_ONLY_RUNS.discard(agent_id)
    except Exception as exc:
        text = f"Reminder: {instruction}\n\n(I couldn't prepare this one: {str(exc)[:200]})"
        vault.append_turn(agent_id, CONVERSATION_ID, "assistant", text)
    return _push(agent_id, reminder, text.strip()[:MAX_MESSAGE_CHARS])


def run_due_once() -> int:
    sent = 0
    for agent_id in list(AGENTS):
        try:
            due = take_due(agent_id)
        except Exception as exc:
            print(f"reminders: couldn't read {agent_id}'s reminders: {exc}")
            continue
        for reminder in due:
            if reminder.get("compose"):
                # Takes as long as an agent reply: off the 30s loop.
                threading.Thread(target=_deliver_logged, args=(agent_id, reminder), daemon=True).start()
                sent += 1
                continue
            try:
                deliver(agent_id, reminder)
                sent += 1
            except Exception as exc:
                print(f"reminders: delivering {agent_id}/{reminder.get('id')} failed: {exc}")
    return sent


def _deliver_logged(agent_id: str, reminder: dict) -> None:
    try:
        deliver(agent_id, reminder)
    except Exception as exc:
        print(f"reminders: delivering {agent_id}/{reminder.get('id')} failed: {exc}")


CHECK_EVERY_S = 30


def start(interval_s: int = CHECK_EVERY_S) -> None:
    """Registers the reminder check with the scheduler: every interval_s
    seconds from startup. Sending pushes can wait on the network, so it runs
    in the scheduler's worker pool."""
    import scheduler
    scheduler.register("reminders", "Send due reminders", run_due_once, every=interval_s, worker=True)
