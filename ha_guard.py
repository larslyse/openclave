"""What R2 may do in Home Assistant without asking (The user, 2026-10-04: option b).

Enforced in code for the HA admin tools (mcp_ha_server.py), on every route:
  - reads: always
  - everyday control (lights, switches, climate, scenes, media, helpers ...): always
  - everything else (config changes, deletions, restarts, locks, alarms,
    scripts and automations, unknown calls): only after the user says yes

Flow: R2 says what it will change and asks (or tries, is refused, and then
asks). When the user's next message starts with a yes, the turn that answers it
may make the changes it described: the whole batch, closed when that turn
ends (chat_service calls end_turn), after YES_WINDOW_S, or after
YES_WINDOW_CALLS as a guard against a runaway loop. Any other reply closes
it. A refused call is also kept as pending and a yes approves it once.
Guards against mistakes, not a hostile model: it can't stop a model that
goes around the tools.

Approval ids (2026-11-05): every refused call becomes a request with a short
id (e.g. AXFG), its exact action, the conversation it was asked in and a
status. "approve AXFG" approves that one call, from any chat, once, for
REQUEST_TTL_S. A plain yes only approves requests asked in the same
conversation, and only until another message goes by. Unknown, used or
expired ids are refused with the reason, so the model tells the owner.
"""
import hashlib
import json
import random
import re
import threading
import time
from config import CONFIG
from state_io import load_json, save_json
import agents


def _has_home_assistant(agent_id: str) -> bool:
    return agents.is_home_assistant_agent(agent_id)
STATE_FILE = CONFIG.state_path("ha_approvals.json")
PENDING_TTL_S = 30 * 60
REQUEST_TTL_S = 24 * 3600   # how long "approve <id>" works
REQUESTS_KEPT = 50
ID_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # no I or O
APPROVE_ID = re.compile(r"\b(?:approve[sd]?|godkjenn(?:er)?|godkjent)\s+([A-Za-z]{4})\b", re.IGNORECASE)
APPROVED_TTL_S = 15 * 60
# The owner's yes approves R2's changes in the turn that answers it (2026-10-04):
# R2 usually asks *before* trying, so a yes that only matched an earlier
# refused call counted for nothing and every action cost two or three yeses.
# Was 5 calls: a described batch (entity renames + a dashboard) ran out
# halfway and left HA half-migrated, twice on 2026-10-13 (R2's flag). A yes
# now covers the turn; the cap only stops a loop.
YES_WINDOW_S = 10 * 60
YES_WINDOW_CALLS = 40
_lock = threading.Lock()

# Service domains the owner uses day to day. Not here on purpose: lock and
# alarm_control_panel (security), script and automation (can do anything),
# and anything unknown.
EVERYDAY_DOMAINS = {
    "light", "switch", "fan", "cover", "climate", "scene", "media_player", "humidifier",
    "water_heater", "vacuum", "remote", "siren", "button", "number", "select", "notify",
    "input_boolean", "input_number", "input_select", "input_button", "input_text", "input_datetime",
}
# homeassistant.* is fine for switching things; restart/stop/reload need a yes.
EVERYDAY_HOMEASSISTANT = {"turn_on", "turn_off", "toggle", "update_entity", "check_config"}  # check_config only validates
# POSTs that only read.
READ_POSTS = {"/api/template", "/api/config/core/check_config"}
# A WebSocket command reads unless one of its parts is a change verb. An
# allow-list of read words kept missing real reads ("lovelace/config",
# "zha/network/settings") and trapped R2 in yes-loops on 2026-10-04.
_WS_WRITE = re.compile(r"(^|[/_])(create|update|remove|delete|save|set|add|reload|restart|install|uninstall|enable|"
                       r"disable|rename|migrate|reconfigure|start|stop|permit|execute|fire|clear|reset|write|import|"
                       r"move|assign|unassign|reorder|purge|ignore|generate|process|run|expose|trigger|toggle|turn|"
                       r"change|edit|replace|upgrade|flash|pair|unpair|join|leave|kick|bind|unbind|apply|commit|"
                       r"restore|revoke|approve|link|unlink|form|erase|wipe|backups?/restore)($|[/_])")

AFFIRMATIVE = re.compile(r"^\s*(yes|yep|yeah|y|ok|okay|sure|go( ahead)?|do it|confirm(ed)?|approved?|ja|jepp|jo|gjør det|kjør)\b", re.IGNORECASE)


def _service_ok(domain: str, service: str) -> bool:
    if domain == "homeassistant":
        return service in EVERYDAY_HOMEASSISTANT
    return domain in EVERYDAY_DOMAINS


def needs_yes_rest(method: str, path: str) -> bool:
    method = method.upper()
    if method == "GET":
        return False
    path = path.split("?", 1)[0].rstrip("/")
    if method == "POST" and path in READ_POSTS:
        return False
    m = re.fullmatch(r"/api/services/([a-z_]+)/([a-z_]+)", path)
    if method == "POST" and m:
        return not _service_ok(*m.groups())
    return True


def needs_yes_ws(message: dict) -> bool:
    kind = str(message.get("type", ""))
    if kind == "call_service":
        return not _service_ok(str(message.get("domain", "")), str(message.get("service", "")))
    return not is_read_ws(message)


def is_read_rest(method: str, path: str) -> bool:
    method = method.upper()
    return method == "GET" or (method == "POST" and path.split("?", 1)[0].rstrip("/") in READ_POSTS)


def is_read_ws(message: dict) -> bool:
    kind = str(message.get("type", ""))
    return bool(kind) and kind != "call_service" and not _WS_WRITE.search(kind)


# Set in the HA server's env for scheduled-reminder runs (reminders.py):
# look things up only, change nothing, not even a light.
READ_ONLY_ENV = "OPENCLAVE_HA_READ_ONLY"
READ_ONLY_REFUSAL = ("NOT DONE — this is a scheduled reminder run, which may only look things up in Home Assistant. "
                     "Say in your message what you would change, and the user can ask you in a chat.")


# Set in the HA server's env for scheduled-task runs (scheduled_tasks.py): a
# comma list of actions the task may take without a yes, each an HA service
# ("update.install") or a WebSocket command type ("backup/generate"); "*" is
# every action (a task the owner trusts with full autonomy).
PREAPPROVED_ENV = "OPENCLAVE_HA_PREAPPROVED"


def action_name(call: dict) -> str:
    """The name a task's pre-approved list matches: domain.service for a
    service call on either API, else the WS command type or "METHOD path"."""
    if call.get("api") == "ws":
        msg = call.get("message") or {}
        if msg.get("type") == "call_service":
            return f"{msg.get('domain', '')}.{msg.get('service', '')}"
        return str(msg.get("type", ""))
    path = str(call.get("path", "")).split("?", 1)[0].rstrip("/")
    m = re.fullmatch(r"/api/services/([a-z_]+)/([a-z_]+)", path)
    if m and str(call.get("method", "")).upper() == "POST":
        return f"{m.group(1)}.{m.group(2)}"
    return f"{str(call.get('method', '')).upper()} {path}"


def preapproved(call: dict, allowed: str | None) -> bool:
    names = {a.strip().lower() for a in (allowed or "").split(",") if a.strip()}
    return "*" in names or (bool(names) and action_name(call).lower() in names)


def call_key(call: dict) -> str:
    """What a yes approves: the action and its target, not every detail of
    the payload. R2 rebuilds e.g. a dashboard save after the user says yes, and an
    exact-payload match refused it as a new change (2026-10-04)."""
    if call.get("api") == "ws":
        msg = call.get("message") or {}
        target = {k: v for k, v in msg.items() if isinstance(v, (str, int, float, bool)) and len(str(v)) < 200}
    else:
        body = call.get("body") or {}
        target = {"method": call.get("method"), "path": call.get("path"),
                  "entity_id": body.get("entity_id") if isinstance(body, dict) else None}
    return hashlib.sha256(json.dumps({"api": call.get("api"), **target}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _load() -> dict:
    data = load_json(STATE_FILE, {})
    if not isinstance(data, dict):
        data = {}
    now = time.time()
    window = data.get("window") or {}
    open_ = window.get("until", 0) > now
    return {
        "requests": [r for r in data.get("requests", []) if now - r["at"] < 7 * 86400][-REQUESTS_KEPT:],
        "conversation": data.get("conversation"),
        "pending": [p for p in data.get("pending", []) if now - p["at"] < PENDING_TTL_S],
        "approved": [a for a in data.get("approved", []) if now - a["at"] < APPROVED_TTL_S],
        "window": window if open_ and window.get("left", 0) > 0 else None,
        # The yes-turn used up its calls (says so in the refusal); kept until the turn ends.
        "spent": window if window and open_ and window.get("left", 0) <= 0 else None,
    }


def _save(data: dict) -> None:
    data = {"pending": data["pending"], "approved": data["approved"],
            "window": data.get("window") or data.get("spent"),
            "requests": data.get("requests", []), "conversation": data.get("conversation")}
    save_json(STATE_FILE, data, indent=1)


def check(call: dict, summary: str) -> str | None:
    """None when the call may run (consuming its approval if it needed one);
    otherwise the refusal text for R2. Call only for calls that need a yes."""
    key = call_key(call)
    with _lock:
        data = _load()
        if any(a["key"] == key for a in data["approved"]):
            data["approved"] = [a for a in data["approved"] if a["key"] != key]
            _save(data)
            return None
        if data["window"]:
            data["window"]["left"] -= 1
            data["window"].setdefault("used", []).append(summary[:120])
            _save(data)
            return None
        spent = data.get("spent")
        if not any(p["key"] == key for p in data["pending"]):
            data["pending"].append({"key": key, "summary": summary[:300], "at": time.time()})
        request = _request_for(data, key, call, summary)
        _save(data)
    if spent:
        return (f"NOT DONE — this turn has already made {YES_WINDOW_CALLS} confirmed changes, the most one yes "
                "allows. Stop here, tell the user exactly what is done and what is left, and ask them to say yes "
                "to the rest.")
    return (f"NOT DONE — the owner must confirm this first (approval id {request['id']}). It changes Home Assistant "
            "beyond everyday control (config, deletion, restart, lock/alarm, or a script/automation). Tell the owner in "
            "one or two plain sentences exactly what you will change, give the id, and ask them to reply yes (or "
            f"'approve {request['id']}' from any chat within a day). Don't retry in this turn. When they say yes, "
            "your next turn may make it (and the related changes you described).")


def _new_id(data: dict) -> str:
    taken = {r["id"] for r in data["requests"]}
    while True:
        rid = "".join(random.choice(ID_LETTERS) for _ in range(4))
        if rid not in taken:
            return rid


def _request_for(data: dict, key: str, call: dict, summary: str) -> dict:
    """The open request for this exact action, or a new one."""
    now = time.time()
    for r in data["requests"]:
        if r["key"] == key and r["status"] == "pending" and now - r["at"] < REQUEST_TTL_S:
            return r
    r = {"id": _new_id(data), "key": key, "action": action_name(call), "summary": summary[:300],
         "call": json.dumps(call, sort_keys=True)[:600], "conversation": data.get("conversation"),
         "status": "pending", "at": now, "yes_ok": True}
    data["requests"].append(r)
    return r


def requests(status: str | None = None) -> list[dict]:
    """Recent approval requests, newest first (Control Panel, tests)."""
    with _lock:
        data = _load()
    now = time.time()
    out = []
    for r in reversed(data["requests"]):
        r = {k: v for k, v in r.items() if k not in ("key", "yes_ok")}
        if r["status"] == "pending" and now - r["at"] >= REQUEST_TTL_S:
            r["status"] = "expired"
        if status is None or r["status"] == status:
            out.append(r)
    return out


def end_turn(agent_id: str) -> None:
    """The turn that answered a yes is over: its approval ends with it, so a
    later turn can't make changes the user hasn't seen described."""
    if not _has_home_assistant(agent_id):
        return
    with _lock:
        data = _load()
        if data["window"] or data["spent"]:
            data["window"] = data["spent"] = None
            _save(data)


def on_user_message(agent_id: str, message: str, conversation_id: str | None = None) -> int:
    """The user's new message to R2: a yes approves the pending calls, anything
    else cancels them. Returns how many were approved."""
    return on_owner_message(agent_id, message, conversation_id)[0]


def on_owner_message(agent_id: str, message: str, conversation_id: str | None = None) -> tuple[int, str]:
    """(approved count, a note for the model or ""). "approve <id>" approves
    that one request from any chat; a plain yes approves what was asked in
    this conversation and opens the yes window; anything else closes it."""
    if not _has_home_assistant(agent_id):
        return 0, ""
    with _lock:
        data = _load()
        now = time.time()
        data["conversation"] = conversation_id
        by_id = APPROVE_ID.search(message or "")
        if by_id:
            rid = by_id.group(1).upper()
            found = next((r for r in data["requests"] if r["id"] == rid), None)
            note = ""
            if not found:
                note = f"The owner wrote 'approve {rid}', but there is no approval request {rid}. Make no change; tell them and ask again."
            elif found["status"] != "pending":
                note = f"The owner wrote 'approve {rid}', but request {rid} ({found['summary']}) was already {found['status']}. Make no change; say so."
            elif now - found["at"] >= REQUEST_TTL_S:
                found["status"] = "expired"
                note = f"The owner wrote 'approve {rid}', but request {rid} ({found['summary']}) is older than a day and expired. Make no change; check the current state and ask again."
            approved = 0
            if not note:
                found["status"] = "approved"
                found["approved_in"] = conversation_id
                data["approved"].append({"key": found["key"], "summary": found["summary"], "at": now, "id": rid})
                approved = 1
                note = (f"The owner approved request {rid}: {found['summary']} (exact call: {found['call']}). First say in one "
                        "line exactly what you will do, then make that one call. Anything else that needs a yes still "
                        "needs its own approval.")
            data["window"] = data["spent"] = None
            data["pending"] = []
            _save(data)
            return approved, note
        yes = bool(AFFIRMATIVE.match(message or ""))
        approved = 0
        if yes:
            here = [r for r in data["requests"] if r["status"] == "pending" and r.get("yes_ok")
                    and r.get("conversation") == conversation_id and now - r["at"] < REQUEST_TTL_S]
            for r in here:
                r["status"] = "approved"
                r["approved_in"] = conversation_id
            keys = {r["key"] for r in here} | {p["key"] for p in data["pending"]
                                               if conversation_id is None or any(p["key"] == r["key"] for r in here)}
            data["approved"] += [{"key": k, "summary": "", "at": now} for k in keys]
            approved = len(keys)
            # And whatever R2 proposed before asking: this turn may make it.
            data["window"] = {"until": now + YES_WINDOW_S, "left": YES_WINDOW_CALLS, "at": now}
        else:
            data["window"] = None
        # A plain yes only answers the question just asked: after any other
        # message, the open requests need "approve <id>".
        for r in data["requests"]:
            if r["status"] == "pending":
                r["yes_ok"] = False if not yes else r.get("yes_ok", False)
        data["spent"] = None
        data["pending"] = []
        _save(data)
        return approved, ""
