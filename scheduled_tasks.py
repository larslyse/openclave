"""Scheduled tasks: agents wake up on set days and do a job (BACKLOG T1).

A task belongs to one agent. It has a title, instructions, the weekdays and
local time it runs, and optionally a short list of Home Assistant actions it
may take without asking (``preapproved``, e.g. ``update.install``). Unlike
composed reminders (reminders.py), a task run may change things: it runs as
a normal agent turn, with the HA guard still asking for a yes on anything
outside its pre-approved list.

Each task reports in its own conversation (``task-<id>``), so the chat is
the run log, and a question the agent asks there ("this broke, may I fix
it?") gets answered right there. Every run also sends a push. The reply's
first line is a status the overview shows: ``STATUS: ok``, ``STATUS: needs
you`` or ``STATUS: failed``.

Stored per agent in its own folder (tasks.json), like reminders.json, so it
is backed up with the agents and visible in the Files tab. Times are local
in the configured timezone.
"""
import json
import re
import threading
import uuid
from datetime import datetime, timedelta

import vault
import reminders
from agents import AGENTS

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
CONVERSATION_PREFIX = "task-"
STATUSES = ("ok", "needs_you", "failed")
MAX_TASKS = 20
MAX_TITLE_CHARS = 80
MAX_INSTRUCTIONS_CHARS = 4000
MAX_PREAPPROVED = 20
HISTORY_KEEP = 30
# A run missed while the service was down still happens, late, within this
# window; older misses are skipped and logged, so a long outage doesn't fire
# a backlog of stale jobs at once.
LATE_LIMIT = timedelta(hours=6)
_FMT = "%Y-%m-%dT%H:%M"
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
# An HA service ("update.install") or WebSocket command ("backup/generate").
_ACTION_RE = re.compile(r"^[a-z_]+(\.[a-z_]+|(/[a-z_]+)+)$")
_STATUS_RE = re.compile(r"^\s*\**\s*STATUS\s*:\s*\**\s*(ok|needs[ _]you|failed)\b\**[ \t.]*\n?", re.IGNORECASE)

_running: set[tuple[str, str]] = set()
_running_lock = threading.Lock()


def conversation_id(task_id: str) -> str:
    return CONVERSATION_PREFIX + task_id


def is_task_conversation(conv_id: str) -> bool:
    return str(conv_id).startswith(CONVERSATION_PREFIX)


def _file(agent_id: str):
    return vault.agent_root(agent_id) / "tasks.json"


def _load(agent_id: str) -> dict:
    try:
        data = json.loads(_file(agent_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"tasks": [t for t in data.get("tasks", []) if isinstance(t, dict) and t.get("id")]}


def _save_unlocked(path, data: dict) -> None:
    vault._write_text_unlocked(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def next_run(days: list[str], at: str, after: datetime) -> datetime:
    """The first matching weekday and time strictly after ``after``."""
    hour, minute = (int(p) for p in at.split(":"))
    wanted = {DAYS.index(d) for d in days}
    for offset in range(8):
        day = after + timedelta(days=offset)
        when = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when.weekday() in wanted and when > after:
            return when
    raise ValueError("no matching day")  # unreachable with at least one day


def _clean(fields: dict, current: dict | None = None) -> dict:
    """Validate the editable fields; raises ValueError with a message for the user."""
    cur = current or {}
    title = str(fields.get("title", cur.get("title", ""))).strip()
    if not title:
        raise ValueError("give the task a title")
    if len(title) > MAX_TITLE_CHARS:
        raise ValueError(f"keep the title under {MAX_TITLE_CHARS} characters")
    instructions = str(fields.get("instructions", cur.get("instructions", ""))).strip()
    if not instructions:
        raise ValueError("say what the agent should do")
    if len(instructions) > MAX_INSTRUCTIONS_CHARS:
        raise ValueError(f"keep the instructions under {MAX_INSTRUCTIONS_CHARS} characters")
    days = fields.get("days", cur.get("days", []))
    if isinstance(days, str):
        days = [d for d in re.split(r"[\s,]+", days) if d]
    days = [str(d).strip().lower()[:3] for d in days]
    if not days or any(d not in DAYS for d in days):
        raise ValueError("pick at least one weekday (mon … sun)")
    days = [d for d in DAYS if d in days]
    at = str(fields.get("time", cur.get("time", ""))).strip()
    m = _TIME_RE.match(at)
    if not m:
        raise ValueError("time must be HH:MM, 24-hour")
    at = f"{int(m.group(1)):02d}:{m.group(2)}"
    pre = fields.get("preapproved", cur.get("preapproved", []))
    if isinstance(pre, str):
        pre = [p for p in re.split(r"[\s,]+", pre) if p]
    pre = list(dict.fromkeys(str(p).strip().lower() for p in pre if str(p).strip()))
    bad = [p for p in pre if p != "*" and not _ACTION_RE.match(p)]
    if bad:
        raise ValueError(f"not an HA action: {', '.join(bad)} (use domain.service like update.install, "
                         "or a WebSocket command like backup/generate)")
    if len(pre) > MAX_PREAPPROVED:
        raise ValueError(f"at most {MAX_PREAPPROVED} pre-approved actions")
    enabled = fields.get("enabled", cur.get("enabled", True))
    return {"title": title, "instructions": instructions, "days": days, "time": at,
            "preapproved": pre, "enabled": bool(enabled)}


def describe_schedule(task: dict) -> str:
    days = task.get("days") or []
    if len(days) == 7:
        label = "Every day"
    elif days == ["mon", "tue", "wed", "thu", "fri"]:
        label = "Weekdays"
    elif len(days) == 1:
        label = "Every " + DAY_NAMES[DAYS.index(days[0])]
    else:
        label = ", ".join(DAY_NAMES[DAYS.index(d)][:3] for d in days)
    return f"{label} at {task.get('time', '')}"


def _public(agent_id: str, task: dict) -> dict:
    history = task.get("history") or []
    return {
        "id": task["id"], "agent_id": agent_id, "title": task.get("title", ""),
        "instructions": task.get("instructions", ""), "days": task.get("days", []), "time": task.get("time", ""),
        "schedule": describe_schedule(task), "preapproved": task.get("preapproved", []),
        "enabled": bool(task.get("enabled", True)),
        "next_at": task.get("next_at") if task.get("enabled", True) else None,
        "conversation_id": conversation_id(task["id"]), "created": task.get("created"),
        "last_run": history[-1] if history else None, "history": list(reversed(history)),
        "running": (agent_id, task["id"]) in _running,
    }


def list_for(agent_id: str) -> list[dict]:
    return [_public(agent_id, t) for t in _load(agent_id)["tasks"]]


def get(agent_id: str, task_id: str) -> dict | None:
    return next((t for t in list_for(agent_id) if t["id"] == task_id), None)


def add(agent_id: str, fields: dict) -> dict:
    task = _clean(fields)
    now = reminders.now_local()
    task.update(id=uuid.uuid4().hex[:8], created=now.strftime(_FMT), history=[],
                next_at=next_run(task["days"], task["time"], now).strftime(_FMT))
    path = _file(agent_id)
    with vault.file_lock(agent_id, path) as scoped:
        data = _load(agent_id)
        if len(data["tasks"]) >= MAX_TASKS:
            raise ValueError(f"already {MAX_TASKS} tasks for this agent")
        data["tasks"].append(task)
        _save_unlocked(scoped, data)
    return _public(agent_id, task)


def update(agent_id: str, task_id: str, fields: dict) -> dict | None:
    path = _file(agent_id)
    if not path.exists():
        return None
    with vault.file_lock(agent_id, path) as scoped:
        data = _load(agent_id)
        for i, task in enumerate(data["tasks"]):
            if task["id"] != task_id:
                continue
            new = {**task, **_clean(fields, task)}
            if (new["days"], new["time"], new["enabled"]) != (task.get("days"), task.get("time"), task.get("enabled", True)) \
                    or not task.get("next_at"):
                new["next_at"] = next_run(new["days"], new["time"], reminders.now_local()).strftime(_FMT)
            data["tasks"][i] = new
            _save_unlocked(scoped, data)
            return _public(agent_id, new)
    return None


def delete(agent_id: str, task_id: str) -> bool:
    """Removes the task; its conversation stays as the record of past runs."""
    path = _file(agent_id)
    if not path.exists():
        return False
    with vault.file_lock(agent_id, path) as scoped:
        data = _load(agent_id)
        kept = [t for t in data["tasks"] if t["id"] != task_id]
        if len(kept) == len(data["tasks"]):
            return False
        data["tasks"] = kept
        _save_unlocked(scoped, data)
    return True


def take_due(agent_id: str, now: datetime | None = None) -> list[dict]:
    """Move due tasks to their next time and return the ones to run now. A
    run missed by more than LATE_LIMIT is logged as missed, not run."""
    path = _file(agent_id)
    if not path.exists():
        return []
    now = now or reminders.now_local()
    stamp = now.strftime(_FMT)
    due = []
    with vault.file_lock(agent_id, path) as scoped:
        data = _load(agent_id)
        changed = False
        for task in data["tasks"]:
            if not task.get("enabled", True) or not task.get("next_at") or str(task["next_at"]) > stamp:
                continue
            try:
                scheduled = datetime.strptime(str(task["next_at"]), _FMT)
                task["next_at"] = next_run(task["days"], task["time"], now).strftime(_FMT)
            except (ValueError, KeyError, TypeError):
                task["enabled"] = False  # hand-edited into something unreadable: pause rather than loop
                changed = True
                continue
            changed = True
            if now - scheduled > LATE_LIMIT:
                _append_history(task, {"scheduled_for": scheduled.strftime(_FMT), "started": None,
                                       "status": "missed", "summary": "Skipped: the service was down at that time."})
                continue
            due.append({**task, "scheduled_for": scheduled.strftime(_FMT)})
        if changed:
            _save_unlocked(scoped, data)
    return due


def _append_history(task: dict, entry: dict) -> None:
    task["history"] = ((task.get("history") or []) + [entry])[-HISTORY_KEEP:]


def _record(agent_id: str, task_id: str, entry: dict) -> None:
    path = _file(agent_id)
    with vault.file_lock(agent_id, path) as scoped:
        data = _load(agent_id)
        for task in data["tasks"]:
            if task["id"] == task_id:
                _append_history(task, entry)
                _save_unlocked(scoped, data)
                return


def split_status(reply: str) -> tuple[str, str]:
    """(status, text without the status line). No status line counts as ok."""
    m = _STATUS_RE.match(reply or "")
    if not m:
        return "ok", (reply or "").strip()
    status = m.group(1).lower().replace(" ", "_")
    return status, (reply[m.end():]).strip()


def summary_of(text: str, limit: int = 300) -> str:
    plain = re.sub(r"[#*_`>]+", "", text or "").strip()
    first = next((line.strip() for line in plain.splitlines() if line.strip()), "")
    return first[:limit]


def run_prompt(task: dict) -> str:
    pre = task.get("preapproved") or []
    if "*" in pre:
        allowed = ("For this task you may make any Home Assistant change without asking, including config "
                   "changes and restarts; say in your report exactly what you changed.")
    elif pre:
        allowed = (f"For this task you may do these Home Assistant actions without asking: {', '.join(pre)}. "
                   "Anything else that would normally need a yes still does.")
    else:
        allowed = ""
    return (
        f"(Scheduled task: {task.get('title', '')}) {task.get('instructions', '')}\n\n"
        "This is a scheduled task you were given; it is running now and nobody is waiting in the chat. "
        "Do the work with your tools. "
        f"{allowed} "
        "When something needs the owner's decision or a yes, describe exactly what you would change and ask: "
        "they will answer in this conversation. "
        "Start your reply with one line: 'STATUS: ok' (done, nothing needed), 'STATUS: needs you' (waiting on "
        "the owner) or 'STATUS: failed' (couldn't do it). Then a short report: what you did, what you found, "
        "and anything left open."
    ).replace("  ", " ")


def _push(agent_id: str, task: dict, status: str, text: str) -> int:
    import web_push
    mark = {"needs_you": "Needs you: ", "failed": "Failed: "}.get(status, "")
    return web_push.send_all(
        title=f"{AGENTS[agent_id].name} · {task.get('title', 'Task')}", body=(mark + summary_of(text, 180))[:240],
        url=f"/mobile/#/agents/{agent_id}/conversations/{conversation_id(task['id'])}",
        tag=f"openclave-task-{agent_id}-{task['id']}",
    )


def run(agent_id: str, task: dict, *, manual: bool = False) -> dict:
    """Run one task now in its conversation and record the result. Waits its
    turn behind any chat the agent is busy with."""
    import chat_service
    import llm_client
    key = (agent_id, task["id"])
    with _running_lock:
        if key in _running:
            return {"status": "running"}
        _running.add(key)
    conv = conversation_id(task["id"])
    started = reminders.now_local().strftime(_FMT)
    try:
        if not vault.conversation_file(agent_id, conv).exists():
            vault.append_turn(agent_id, conv, "assistant",
                              f"Scheduled task: {task.get('title', '')}. Each run reports here.",
                              first_message=f"Task: {task.get('title', '')}")
        pre = task.get("preapproved") or []
        try:
            with chat_service.agent_execution(agent_id, wait=True):
                if pre:
                    llm_client.RUN_ENV[agent_id] = {"OPENCLAVE_HA_PREAPPROVED": ",".join(pre)}
                try:
                    reply = chat_service.run_chat_locked(agent_id, conv, run_prompt(task))
                finally:
                    llm_client.RUN_ENV.pop(agent_id, None)
            status, text = split_status(reply)
            if chat_service._is_error_reply(reply):
                status = "failed"
        except Exception as exc:
            status, text = "failed", f"The task couldn't run: {str(exc)[:300]}"
            vault.append_turn(agent_id, conv, "assistant", text)
        entry = {"scheduled_for": task.get("scheduled_for") or started, "started": started,
                 "finished": reminders.now_local().strftime(_FMT), "status": status,
                 "summary": summary_of(text), **({"manual": True} if manual else {})}
        _record(agent_id, task["id"], entry)
        try:
            _push(agent_id, task, status, text)
        except Exception as exc:
            print(f"tasks: push for {agent_id}/{task['id']} failed: {exc}")
        return entry
    finally:
        with _running_lock:
            _running.discard(key)


def run_in_background(agent_id: str, task: dict, *, manual: bool = False) -> None:
    def go():
        try:
            run(agent_id, task, manual=manual)
        except Exception as exc:
            print(f"tasks: running {agent_id}/{task.get('id')} failed: {exc}")
    threading.Thread(target=go, name=f"openclave-task-{agent_id}", daemon=True).start()


def run_due_once() -> int:
    started = 0
    for agent_id in list(AGENTS):
        try:
            due = take_due(agent_id)
        except Exception as exc:
            print(f"tasks: couldn't read {agent_id}'s tasks: {exc}")
            continue
        for task in due:
            run_in_background(agent_id, task)  # takes as long as an agent turn: off the loop
            started += 1
    return started


def prompt_line(agent_id: str) -> str:
    """For the agent's own prompt: the tasks it runs on a schedule. Never raises."""
    try:
        tasks = [t for t in list_for(agent_id) if t["enabled"]]
    except Exception:
        return ""
    if not tasks:
        return ""
    listed = "; ".join(f"'{t['title']}' ({t['schedule']})" for t in tasks)
    return (f"Your scheduled tasks: {listed}. Each reports in its own conversation. They are set up in the "
            "Control Panel; you can't change them yourself, so point there when asked. ")


def overview() -> dict:
    """Control Panel: every agent's tasks."""
    import agent_order
    out = []
    for agent_id in agent_order.get_order():
        if agent_id in AGENTS:
            out.extend({**t, "agent_name": AGENTS[agent_id].name} for t in list_for(agent_id))
    return {"now": reminders.describe_now(), "tasks": out}


CHECK_EVERY_S = 30


def start(interval_s: int = CHECK_EVERY_S) -> None:
    """Registers the scheduled-task check with the scheduler: every
    interval_s seconds from startup. The check itself is quick (each due task
    runs in its own thread), so it runs inline."""
    import scheduler
    scheduler.register("scheduled-tasks", "Start due scheduled tasks", run_due_once, every=interval_s)
