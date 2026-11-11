"""Weekly OpenClave conversation cleanup.

"For OpenClave to work well there should only be a handful of active
conversations per agent, and these should all have very clear names." Once a
week this looks at every agent's conversations and suggests:

  - archive: no activity for STALE_DAYS, or the oldest idle ones when an
    agent has more than TARGET_ACTIVE active conversations
  - delete: a conversation that never got going (no reply, or one trivially
    short exchange) and has been idle for SMALL_IDLE_DAYS; a single real
    exchange idle for SINGLE_IDLE_DAYS is suggested for archiving instead
  - rename: still named after the first line of its first message, with a
    short suggested name (conversation_titles, the same as for new chats)

and flags agents nobody has talked to for DORMANT_DAYS. The rules are plain
file checks; only the suggested names use a model. The maintainer only suggests:
nothing is archived, deleted or renamed until the user applies a suggestion on
the maintainer's phone page (moved there from the Control Panel, 2026-10-14). "Keep" (dismiss) is remembered, so the same suggestion
doesn't come back. The summary arrives in the maintainer's Cleanup
conversation, with a push.
"""
import json
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from datetime import datetime, timedelta

import conversation_titles
import reminders
import scheduled_tasks
import vault
from agents import AGENTS
from config import CONFIG

STALE_DAYS = 30
SMALL_IDLE_DAYS = 7     # delete: no reply, or a trivially short exchange
SINGLE_IDLE_DAYS = 14   # archive: one real exchange, then nothing
TARGET_ACTIVE = 5
ACTIVE_IDLE_DAYS = 7  # over the target, only conversations idle this long are suggested
DORMANT_DAYS = 30
RUN_WEEKDAY, RUN_HOUR, LAST_HOUR = 0, 9, 21  # Mondays 09:00-21:00 in the configured timezone
STATE_FILE = CONFIG.state_path("cleanup_state.json")
CONVERSATION_ID = "cleanup"
# Conversations the service itself writes to: never suggested.
SKIP_IDS = {reminders.CONVERSATION_ID, CONVERSATION_ID, "audit"}

_LOCK = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def _conversation_facts(agent_id: str, item: dict, now_ms: int) -> dict | None:
    conversation = vault.get_conversation(agent_id, item["id"])
    if not conversation:
        return None
    turns = conversation["turns"]
    first_user = next((t["content"] for t in turns if t["role"] == "user"), "")
    exchanges = sum(1 for t in turns if t["role"] == "user")
    return {
        "id": item["id"],
        "title": item["title"],
        "idle_days": max(0, (now_ms - (item.get("updated_ms") or now_ms)) // 86_400_000),
        "exchanges": exchanges,
        "has_reply": any(t["role"] != "user" for t in turns),
        "unnamed": bool(turns) and turns[0]["role"] == "user" and not conversation.get("summary")
        and item["title"] == conversation_titles.initial_title(first_user),
        "first_user": first_user,
        "first_reply": next((t["content"] for t in turns if t["role"] != "user"), ""),
    }


def _finished_onboarding_chat(agent_id: str) -> str | None:
    import agents
    try:
        data = json.loads((vault.agent_root(agent_id) / agents.AGENT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return None
    return None if data.get("onboarding") else data.get("onboarding_conversation") or None


def suggestions_for(agent_id: str, now: datetime | None = None, name=None,
                    dismissed: frozenset = frozenset()) -> tuple[list[dict], list[dict]]:
    """(conversation suggestions, agent notes) for one agent. `name` makes a
    suggested title from (first message, first reply); None skips names."""
    local_now = now or datetime.now(reminders.TZ)
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=reminders.TZ)
    now_ms = int(local_now.timestamp() * 1000)
    items = [i for i in vault.list_conversations(agent_id) if i["id"] not in SKIP_IDS and not scheduled_tasks.is_task_conversation(i["id"])]
    facts = [f for f in (_conversation_facts(agent_id, i, now_ms) for i in items) if f]
    out, taken = [], set()

    def suggest(kind, f, reason, **extra):
        taken.add(f["id"])
        out.append({"id": f"{kind}:{agent_id}:{f['id']}", "kind": kind, "agent_id": agent_id,
                    "conversation_id": f["id"], "title": f["title"], "reason": reason, **extra})

    finished_onboarding = _finished_onboarding_chat(agent_id)
    for f in facts:
        if f["id"] == finished_onboarding and f["idle_days"] >= 1:
            suggest("delete", f, "onboarding is finished; mission, persona and tools hold what came out of it")
            continue
        trivial = len(f["first_user"].strip()) < 40 and len(f["first_reply"].strip()) < 300
        if f["exchanges"] <= 1 and f["idle_days"] >= SMALL_IDLE_DAYS and (not f["has_reply"] or trivial):
            what = "one message, no reply" if not f["has_reply"] else "one short exchange"
            suggest("delete", f, f"{what}, idle {f['idle_days']} days")
        elif f["exchanges"] <= 1 and f["idle_days"] >= SINGLE_IDLE_DAYS:
            suggest("archive", f, f"a single exchange, idle {f['idle_days']} days")
        elif f["idle_days"] >= STALE_DAYS:
            suggest("archive", f, f"no activity for {f['idle_days']} days")
    active = [f for f in facts if f["id"] not in taken]  # newest first, like the list
    if len(active) > TARGET_ACTIVE:
        for f in reversed(active[TARGET_ACTIVE:]):
            if f["idle_days"] >= ACTIVE_IDLE_DAYS:
                suggest("archive", f, f"{len(active)} active conversations (aim for {TARGET_ACTIVE} or fewer); "
                                      f"idle {f['idle_days']} days")
    unnamed = [f for f in facts if f["id"] not in taken and f["unnamed"]
               and f"rename:{agent_id}:{f['id']}" not in dismissed]

    def make_name(f):
        try:
            return name(f["first_user"], f["first_reply"]) or None
        except Exception:  # noqa: BLE001 -- a name is optional
            return None

    if name and unnamed:
        with ThreadPoolExecutor(max_workers=6) as pool:
            names = list(pool.map(make_name, unnamed))
        for f, suggested in zip(unnamed, names):
            if suggested and suggested != f["title"]:
                suggest("rename", f, "unclear name", suggested_title=suggested)

    notes = []
    last = min((f["idle_days"] for f in facts), default=None)
    if last is None or last >= DORMANT_DAYS:
        notes.append({"agent_id": agent_id, "kind": "dormant",
                      "text": "no conversations yet" if last is None else f"no conversation for {last} days"})
    elif len(active) > TARGET_ACTIVE:
        notes.append({"agent_id": agent_id, "kind": "busy", "text": f"{len(active)} active conversations"})
    return out, notes


def _model_name(user_text: str, reply_text: str) -> str:
    return conversation_titles.suggest(user_text, reply_text)


def generate(now: datetime | None = None, name=None) -> dict:
    """Build this week's report (keeping what the user dismissed) and save it."""
    name = _model_name if name is None else name
    now = now or reminders.now_local()
    with _LOCK:
        state = _load()
        dismissed = set(state.get("dismissed", []))
        suggestions, notes = [], []
        # Control Panel → Weekly review: which kinds to suggest.
        import service_settings
        review = service_settings.load()["weekly_review"]
        wanted = {k for k, key in (("archive", "archive_stale"), ("delete", "delete_empty"),
                                   ("rename", "rename_unclear")) if review[key]}
        for agent_id in AGENTS:
            s, n = suggestions_for(agent_id, None, name if "rename" in wanted else None,
                                   frozenset(dismissed))  # idle days from the real clock
            suggestions += [x for x in s if x["id"] not in dismissed and x["kind"] in wanted]
            notes += n if review["flag_agents"] else []
        state.update({"generated_at": now.strftime("%Y-%m-%d %H:%M"), "suggestions": suggestions,
                      "agents": notes, "dismissed": sorted(dismissed)})
        _save(state)
    return state


def report() -> dict:
    state = _load()
    names = {a: AGENTS[a].name for a in AGENTS}
    archived = [{"agent_id": a, "agent_name": names[a], **c}
                for a in AGENTS for c in vault.list_archived_conversations(a)]
    return {
        "generated_at": state.get("generated_at"),
        "suggestions": [{**s, "agent_name": names.get(s["agent_id"], s["agent_id"])}
                        for s in state.get("suggestions", []) if s["agent_id"] in names],
        "agents": [{**n, "agent_name": names.get(n["agent_id"], n["agent_id"])}
                   for n in state.get("agents", []) if n["agent_id"] in names],
        "archived": archived,
        # The thresholds, so an empty report can say what the review looks for.
        "rules": {"stale_days": STALE_DAYS, "small_idle_days": SMALL_IDLE_DAYS,
                  "single_idle_days": SINGLE_IDLE_DAYS, "target_active": TARGET_ACTIVE},
        "last_week": state.get("last_week"),
    }


def _drop(suggestion_id: str, dismiss: bool) -> dict | None:
    with _LOCK:
        state = _load()
        found = next((s for s in state.get("suggestions", []) if s["id"] == suggestion_id), None)
        if found:
            state["suggestions"] = [s for s in state["suggestions"] if s["id"] != suggestion_id]
            if dismiss:
                state["dismissed"] = sorted(set(state.get("dismissed", [])) | {suggestion_id})
            _save(state)
        return found


def apply(suggestion_id: str, title: str | None = None) -> dict:
    """The user confirmed one suggestion. Raises LookupError if it's gone, and
    whatever the action raises (e.g. chat_jobs.AgentBusyError)."""
    import chat_jobs
    found = next((s for s in _load().get("suggestions", []) if s["id"] == suggestion_id), None)
    if not found:
        raise LookupError("that suggestion is no longer there")
    agent_id, cid = found["agent_id"], found["conversation_id"]
    if found["kind"] == "delete":
        done = chat_jobs.delete_conversation(agent_id, cid)
    elif found["kind"] == "archive":
        done = chat_jobs.archive_conversation(agent_id, cid)
    else:
        new_title = (title or found.get("suggested_title") or "").strip()
        if not new_title:
            raise ValueError("a name is needed")
        # Only if the name is still the one the suggestion was made for.
        done = vault.rename_conversation_if(agent_id, cid, found["title"], new_title[:120])
    _drop(suggestion_id, dismiss=False)
    return {"done": bool(done), "kind": found["kind"]}


def dismiss(suggestion_id: str) -> bool:
    return _drop(suggestion_id, dismiss=True) is not None


def summary_text(state: dict) -> str:
    counts = {}
    for s in state.get("suggestions", []):
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    parts = [f"{counts[k]} to {k}" for k in ("archive", "delete", "rename") if counts.get(k)]
    lines = []
    if parts:
        lines.append("Weekly cleanup: " + ", ".join(parts) + ".")
    else:
        lines.append("Weekly cleanup: nothing to tidy this week.")
    for n in state.get("agents", []):
        name = AGENTS[n["agent_id"]].name if n["agent_id"] in AGENTS else n["agent_id"]
        lines.append(f"- {name}: {n['text']}")
    if parts:
        maintainer = next((agent_id for agent_id, cfg in AGENTS.items() if "maintainer" in cfg.roles), None)
        if maintainer:
            lines.append(f"\nNothing changes until you say so: [my page](/mobile/#/agents/{maintainer}/console).")
    return "\n".join(lines)


def run_weekly(now: datetime | None = None) -> dict:
    """Generate, post the summary in the maintainer's Cleanup conversation, push."""
    import web_push
    state = generate(now)
    text = summary_text(state)
    maintainer = next((agent_id for agent_id, cfg in AGENTS.items() if "maintainer" in cfg.roles), None)
    page_url = f"/mobile/#/agents/{maintainer}/console" if maintainer else "/mobile/"
    if maintainer:
        vault.append_turn(maintainer, CONVERSATION_ID, "assistant", text, first_message="Cleanup")
    sent = 0
    if state.get("suggestions") or state.get("agents"):
        sent = web_push.send_all("OpenClave: weekly cleanup", text.split("\n")[0],
                                 url=page_url,
                                 tag="openclave-cleanup")
    with _LOCK:
        st = _load()
        st["last_week"] = (now or reminders.now_local()).strftime("%G-W%V")
        st["last_devices"] = sent
        _save(st)
    # Then the manager's own audit (audit.py): it looks at what the agents
    # wrote this week and proposes what's worth doing, incl. the main vault.
    audited = {}
    try:
        import audit
        audited = audit.run(text.split("\n")[0])
    except Exception as exc:  # noqa: BLE001 -- the cleanup itself is done
        audited = {"queued": False, "reason": str(exc)}
    return {"suggestions": len(state.get("suggestions", [])), "sent": sent, "audit": audited}


def is_due(now: datetime, last_week: str | None) -> bool:
    week = now.strftime("%G-W%V")
    return last_week != week and now.weekday() >= RUN_WEEKDAY and RUN_HOUR <= now.hour < LAST_HOUR


def start(interval_s: int = 300) -> None:
    def loop():
        while True:
            try:
                now = reminders.now_local()
                if is_due(now, _load().get("last_week")):
                    run_weekly(now)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(interval_s)
    threading.Thread(target=loop, name="openclave-cleanup", daemon=True).start()
