"""What needs the user's attention, per agent — deterministic, no LLM.

From the 2026-09-27 evaluation: agents only act when asked, so plans went
stale and nothing said what needed a look. This checks each agent's own files
(cheap file reads, safe to run on every request) and returns at most a few
short findings per agent:

  - tracking gone quiet: an agent that logs measurements has no entry for
    TRACKING_GAP_DAYS (only once the log is really in use)
  - open inbox: the maintainer has open flags from other agents
  - stale plan: plan.md not updated for STALE_PLAN_DAYS
  - untouched backlog: open items and backlog.md not updated for
    STALE_BACKLOG_DAYS
  The last two only for agents the user talked to in the last ACTIVE_DAYS, and
  they are upkeep for the agent, not something the user must do: they show in the
  Control Panel and the agent's own prompt asks it to catch up (upkeep_line),
  but they don't set the amber "needs you" dot or the digest. With them in,
  the dot was on 7 of 10 tiles (2026-10-04), which made it meaningless.

The digest (optional, Control Panel → Settings) pushes one line per agent
that has something, weekly or daily, at a set time in the configured timezone. It only
reads files and sends one notification: it never runs an agent.
"""
import re
import agents
import threading
import time
from datetime import date, datetime
from pathlib import Path
from config import CONFIG
from state_io import load_json, save_json

import claude_auth
import llm_models
import reminders
import service_settings
import vault
from agents import AGENTS

STALE_PLAN_DAYS = 14
STALE_BACKLOG_DAYS = 14
TRACKING_GAP_DAYS = 7
ACTIVE_DAYS = 14
TRACKING_MIN_ENTRIES = 3
STATE_FILE = CONFIG.state_path("digest_state.json")

_DATE_RE = re.compile(r"^\|\s*(\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4})\s*\|", re.M)


def _parse_day(text: str) -> date | None:
    text = str(text or "").strip().strip("'\"")
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _updated(fm_value, path: Path) -> date | None:
    """Frontmatter 'Updated', else the file's modification date."""
    day = _parse_day(fm_value)
    if day is None and path.exists():
        day = datetime.fromtimestamp(path.stat().st_mtime, reminders.TZ).date()
    return day


def _tracking_finding(agent_id: str, today: date) -> dict | None:
    f = vault.tracking_file(agent_id)
    if not f.exists():
        return None
    days = [d for d in (_parse_day(m) for m in _DATE_RE.findall(f.read_text(encoding="utf-8"))) if d]
    if len(days) < TRACKING_MIN_ENTRIES:
        return None
    gap = (today - max(days)).days
    if gap < TRACKING_GAP_DAYS:
        return None
    return {"kind": "tracking", "days": gap, "text": f"No log entry for {gap} days"}


def _last_chat_day(agent_id: str) -> date | None:
    """Last day the user and this agent talked (reminders it sent don't count)."""
    d = vault.conversations_dir(agent_id)
    times = [f.stat().st_mtime for f in (d.glob("*.md") if d.exists() else ()) if f.stem != reminders.CONVERSATION_ID
             and not f.stem.startswith("task-")]
    return datetime.fromtimestamp(max(times), reminders.TZ).date() if times else None


def check(agent_id: str, today: date | None = None) -> list[dict]:
    today = today or reminders.now_local().date()
    out = []
    import connections  # lazy: pulls in llm_client
    missing = connections.lost_now(agent_id)
    if missing:
        model = llm_models.get(llm_models.selected_model_id(agent_id)).display_name
        out.append({"kind": "connection", "text": f"Can't reach {' and '.join(missing)} on {model}: switch to a Claude CLI model"})
    if claude_auth.signed_out() and llm_models.get(llm_models.selected_model_id(agent_id)).provider == "claude-cli":
        out.append({"kind": "claude_login", "text": "Claude CLI is signed out: sign in again in Control Panel → Claude sign-in"})
    finding = _tracking_finding(agent_id, today)
    if finding:
        out.append(finding)
    if agents.has_role(agent_id, "maintainer"):
        open_items = len(vault.read_maintainer_inbox(status="open"))
        if open_items:
            out.append({"kind": "inbox", "count": open_items,
                        "text": f"{open_items} open item{'s' if open_items != 1 else ''} in the inbox"})
    last_chat = _last_chat_day(agent_id)
    if not last_chat or (today - last_chat).days > ACTIVE_DAYS:
        return out
    plan = vault.read_plan(agent_id)
    plan_day = _updated(plan.get("updated"), vault.plan_file(agent_id))
    if plan_day and (today - plan_day).days >= STALE_PLAN_DAYS:
        out.append({"kind": "plan", "upkeep": True, "days": (today - plan_day).days,
                    "text": f"Plan not updated for {(today - plan_day).days} days"})
    backlog = vault.read_backlog(agent_id)
    open_count = sum(1 for i in backlog["items"] if not i["done"])
    backlog_day = _updated(backlog.get("updated"), vault.backlog_file(agent_id))
    if open_count and backlog_day and (today - backlog_day).days >= STALE_BACKLOG_DAYS:
        out.append({"kind": "backlog", "upkeep": True, "count": open_count, "days": (today - backlog_day).days,
                    "text": f"{open_count} open backlog item{'s' if open_count != 1 else ''}, untouched for {(today - backlog_day).days} days"})
    return out


def check_all(today: date | None = None) -> dict[str, list[dict]]:
    results = {}
    for agent_id in list(AGENTS):
        try:
            results[agent_id] = check(agent_id, today)
        except Exception:  # one unreadable file must not hide everyone else
            results[agent_id] = []
    return results


def needs_you(results: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Only the findings the user has to act on (the amber dot, the digest)."""
    return {a: [i for i in items if not i.get("upkeep")] for a, items in results.items()}


def upkeep_line(agent_id: str) -> str:
    """For the agent's own prompt: its plan/backlog are behind. Never raises."""
    try:
        items = [i for i in check(agent_id) if i.get("upkeep")]
    except Exception:
        return ""
    if not items:
        return ""
    what = " and ".join("plan" if i["kind"] == "plan" else "backlog" for i in items)
    days = max(i["days"] for i in items)
    return (f"Your {what} {'is' if len(items) == 1 else 'are'} out of date (last updated {days} days ago). When this "
            "conversation gives you something relevant, bring it up to date with update_plan_phase / update_backlog, "
            "without making a point of it. ")


def digest_lines(results: dict[str, list[dict]]) -> list[str]:
    """One line per agent that has something: its most important finding."""
    lines = []
    for agent_id, items in results.items():
        if items and agent_id in AGENTS:
            extra = f" (+{len(items) - 1} more)" if len(items) > 1 else ""
            lines.append(f"{AGENTS[agent_id].name}: {items[0]['text']}{extra}")
    return lines


# ── digest ──────────────────────────────────────────────────────────────

def _load_state() -> dict:
    data = load_json(STATE_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    save_json(STATE_FILE, state)


def state() -> dict:
    return _load_state()


def is_due(settings: dict, now: datetime, last_sent: str | None) -> bool:
    mode = settings.get("digest", "off")
    if mode not in ("daily", "weekly"):
        return False
    if mode == "weekly" and now.weekday() != 0:  # Mondays
        return False
    hh, mm = (int(x) for x in settings.get("digest_time", "07:30").split(":"))
    if (now.hour, now.minute) < (hh, mm):
        return False
    return last_sent != now.date().isoformat()


def send_digest(force: bool = False) -> dict:
    """Sends the digest now if there's anything to say (or force for a test).
    Returns {"sent": n_devices, "lines": [...]} and records the run."""
    import web_push  # lazy: pulls in crypto only when a digest actually goes out
    now = reminders.now_local()
    lines = digest_lines(needs_you(check_all(now.date())))
    sent = 0
    if lines or force:
        body = "\n".join(lines) if lines else "Nothing needs you right now."
        sent = web_push.send_all("OpenClave: what needs you", body, url="/mobile/", tag="openclave-digest")
    st = _load_state()
    st.update({"last_run": now.strftime("%Y-%m-%d %H:%M"), "last_lines": lines, "last_devices": sent})
    if not force:
        st["last_sent"] = now.date().isoformat()
    _save_state(st)
    return {"sent": sent, "lines": lines}


def run_due_once() -> bool:
    now = reminders.now_local()
    if not is_due(service_settings.load(), now, _load_state().get("last_sent")):
        return False
    send_digest()
    return True


def start(interval_s: int = 60) -> None:
    def loop():
        while True:
            try:
                run_due_once()
            except Exception:
                pass
            time.sleep(interval_s)
    threading.Thread(target=loop, name="openclave-digest", daemon=True).start()
