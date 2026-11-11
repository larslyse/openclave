"""Keeps the model pickers current without the user having to (Rick's backlog,
2026-10-04). Once a day, in the background:

Since 2026-11-04 it no longer adds or hides anything by itself: what it
finds is saved as suggestions (state "available" and "gone"), and the
manager's weekly audit proposes them (audit.model_proposals) when the
Weekly review checklist says so. A Yes on the proposal calls add_available
or hide. Points 1-2 below describe what it looks for.

1. Add: a model a provider offers that is newer than every model OpenClave
   already has from that provider (release date; Codex: its own ordering,
   and never one it calls older/previous/legacy) is test-replied on its
   route. Only a model that answers gets added, with name, image support and
   price from the provider and its speed group from the measured time.
2. Hide (never delete): a model the provider no longer offers, and, when a
   picker group has more than MAX_PER_GROUP models, its oldest ones. A model
   an agent (or the fallback setting) uses, or one the user restored, is never
   hidden.
3. Log every change; Control Panel → Model updates shows them with
   Remove / Restore, and an on/off switch.

Probes call each provider directly (not through an agent), so they never
hold an agent's chat lock or show up as its usage. State: auto_models.json
(gitignored), read by llm_models.apply_auto.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

import llm_client
import llm_models
from llm_models import ModelOption
from state_io import load_json, save_json

STATE_FILE = llm_models.AUTO_FILE
MAX_PER_GROUP = 6
MAX_FAILURES = 3          # test-reply failures before a model is no longer retried
RUN_EVERY_S = 24 * 3600
FIRST_RUN_DELAY_S = 15 * 60
API_FAST_S, CLI_FAST_S = 4.0, 12.0
PROBE_PROMPT = "Reply with just OK"
_OLDER = re.compile(r"^(older|previous|legacy)\b", re.IGNORECASE)
_SKIP = re.compile(r"imagine|image|video|multi-agent|embedding|reserve|auto-review|-\d{8}$")
_lock = threading.Lock()
_running = threading.Event()


# ── state ────────────────────────────────────────────────────────────────

def load() -> dict:
    state = load_json(STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    for key in ("added", "hidden", "pinned", "blocked", "log", "gone", "provider_older"):
        state.setdefault(key, [])
    state.setdefault("available", {})
    state.setdefault("failures", {})
    state.setdefault("enabled", True)
    state.setdefault("last_run", None)
    return state


def save(state: dict) -> None:
    state["log"] = state["log"][-100:]
    save_json(STATE_FILE, state, indent=1)
    llm_models.apply_auto(state)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(state: dict, action: str, model_id: str, name: str, detail: str) -> None:
    state["log"].append({"at": _now(), "action": action, "id": model_id, "name": name, "detail": detail})


# ── what the providers offer ───────────────────────────────────────────────
# Each offer: {route, api_id, name, vision, price_in, price_out, rank, older, tier}
# rank: larger = newer (a timestamp, or for Codex its position from the end).

def _xai() -> list[dict]:
    r = httpx.get("https://api.x.ai/v1/language-models", timeout=15,
                  headers={"Authorization": f"Bearer {os.environ['XAI_API_KEY']}"})
    r.raise_for_status()
    out = []
    for m in r.json()["models"]:
        pretty = " ".join(w.capitalize() if not w[0].isdigit() else w for w in m["id"].split("-"))
        # xAI prices are in 1/10,000 USD per million tokens.
        price = lambda v: round(v / 10000, 2) if isinstance(v, (int, float)) else None
        out.append({"route": "xai", "api_id": m["id"], "name": pretty, "vision": "image" in (m.get("input_modalities") or []),
                    "price_in": price(m.get("prompt_text_token_price")), "price_out": price(m.get("completion_text_token_price")),
                    "rank": float(m.get("created") or 0), "older": False, "tier": "new xAI model"})
    return out


def _anthropic() -> list[dict]:
    import anthropic
    out = []
    for m in anthropic.Anthropic().models.list(limit=100):
        d = m.model_dump()
        vision = bool(((d.get("capabilities") or {}).get("image_input") or {}).get("supported"))
        base = {"api_id": d["id"], "vision": vision, "price_in": None, "price_out": None,
                "rank": d["created_at"].timestamp() if d.get("created_at") else 0.0, "older": False}
        out.append({**base, "route": "anthropic", "name": d.get("display_name") or d["id"], "tier": "new Anthropic model"})
        out.append({**base, "route": "claude-cli", "name": f"{d.get('display_name') or d['id']} (subscription)",
                    "tier": "new Anthropic model, on the Claude subscription"})
    return out


def _codex() -> list[dict]:
    raw = subprocess.run([llm_client.CODEX_BIN, "debug", "models"], capture_output=True, text=True, timeout=60).stdout
    listed = [m for m in json.loads(raw)["models"] if m.get("visibility") == "list"]
    out = []
    for i, m in enumerate(listed):
        name = re.sub(r"^GPT-(\S+?)-", r"GPT-\1 ", m.get("display_name") or m["slug"])  # "GPT-6.1-Sol" -> "GPT-6.1 Sol"
        out.append({"route": "codex-cli", "api_id": m["slug"], "name": name, "vision": "image" in (m.get("input_modalities") or []),
                    "price_in": None, "price_out": None, "rank": float(len(listed) - i),
                    "older": bool(_OLDER.match(m.get("description") or "")), "tier": (m.get("description") or "new Codex model").rstrip(".")[:60]})
    return out


SOURCES = {"xai": _xai, "anthropic": _anthropic, "codex-cli": _codex}  # anthropic covers claude-cli too


def offers() -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Offers per route, and an error per source that couldn't be read."""
    by_route: dict[str, list[dict]] = {}
    errors = {}
    for name, fetch in SOURCES.items():
        try:
            for o in fetch():
                if not _SKIP.search(o["api_id"]):
                    by_route.setdefault(o["route"], []).append(o)
        except Exception as exc:
            errors[name] = type(exc).__name__
    return by_route, errors


# ── probes: one test reply, straight to the provider ──────────────────────

def _probe(route: str, api_id: str) -> float:
    """Seconds for a trivial reply; raises if the model doesn't answer OK."""
    start = time.perf_counter()
    if route == "xai":
        r = httpx.post("https://api.x.ai/v1/chat/completions", timeout=90,
                       headers={"Authorization": f"Bearer {os.environ['XAI_API_KEY']}"},
                       json={"model": api_id, "messages": [{"role": "user", "content": PROBE_PROMPT}]})
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
    elif route == "anthropic":
        import anthropic
        msg = anthropic.Anthropic().messages.create(model=api_id, max_tokens=2000,
                                                    messages=[{"role": "user", "content": PROBE_PROMPT}])
        text = "".join(b.text for b in msg.content if b.type == "text")
    elif route == "claude-cli":
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        res = subprocess.run([llm_client.CLAUDE_BIN, "-p", PROBE_PROMPT, "--model", api_id, "--setting-sources", "",
                              "--no-session-persistence", "--tools", ""],
                             capture_output=True, text=True, timeout=180, env=env, cwd=tempfile.gettempdir())
        if res.returncode:
            raise RuntimeError(llm_client._cli_diagnostic(res.stderr or res.stdout, res.returncode))
        text = res.stdout
    elif route == "codex-cli":
        env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "XDG_CONFIG_HOME", "XDG_CACHE_HOME")}
        home = tempfile.mkdtemp(prefix="openclave-probe-")
        try:
            source_auth = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
            if source_auth.is_file():
                (Path(home) / "auth.json").symlink_to(source_auth)
            env["CODEX_HOME"] = home
            res = subprocess.run([llm_client.CODEX_BIN, "exec", "--skip-git-repo-check", "-s", "read-only", "-C", home,
                                  "-m", api_id, "--", PROBE_PROMPT],
                                 capture_output=True, text=True, timeout=180, env=env, stdin=subprocess.DEVNULL)
        finally:
            shutil.rmtree(home, ignore_errors=True)
        if res.returncode:
            raise RuntimeError(llm_client._cli_diagnostic(res.stderr, res.returncode))
        text = res.stdout
    else:
        raise ValueError(route)
    if "ok" not in (text or "").lower():
        raise RuntimeError(f"unexpected reply: {(text or '').strip()[:80]!r}")
    return time.perf_counter() - start


# ── one run ───────────────────────────────────────────────────────────────

def _catalog_id(route: str, api_id: str) -> str:
    return f"{api_id}-cli" if route == "claude-cli" else api_id


def _known_api_ids(route: str) -> set[str]:
    return {m.api_model_id or m.id for m in llm_models.CATALOG if m.provider == route}


def _candidates(state: dict, by_route: dict[str, list[dict]]) -> list[dict]:
    blocked = set(state["blocked"])
    out = []
    for route, offered in by_route.items():
        known = _known_api_ids(route)
        newest = max((o["rank"] for o in offered if o["api_id"] in known), default=0.0)
        for o in offered:
            cid = _catalog_id(route, o["api_id"])
            if o["api_id"] in known or cid in blocked or o["older"] or o["rank"] <= newest or cid in state["available"]:
                continue
            if state["failures"].get(cid, 0) >= MAX_FAILURES:
                continue
            out.append(o)
    return out


def _model_from_offer(o: dict, seconds: float) -> ModelOption:
    api = o["route"] in ("xai", "anthropic")
    fast = seconds < (API_FAST_S if api else CLI_FAST_S)
    cid = _catalog_id(o["route"], o["api_id"])
    model = ModelOption(
        id=cid, provider=o["route"], display_name=o["name"], tier=o["tier"],
        api_model_id=o["api_id"] if cid != o["api_id"] else None, supports_vision=o["vision"],
        api_input_usd=o["price_in"], api_output_usd=o["price_out"], speed="fast" if fast else "slow",
        auto_added=datetime.now(timezone.utc).date().isoformat(),
        released=(datetime.fromtimestamp(o["rank"], timezone.utc).strftime("%Y-%m") if o["rank"] > 1e6
                  else datetime.now(timezone.utc).strftime("%Y-%m")),
    )
    return model


def _offer_found(state: dict, o: dict, seconds: float) -> None:
    """A new model that answered its test reply: kept as a suggestion."""
    model = _model_from_offer(o, seconds)
    state["failures"].pop(model.id, None)
    price = f", ${o['price_in']}/${o['price_out']} per M" if o["price_in"] is not None else ""
    detail = f"test reply in {seconds:.1f}s → {model.route} · {model.speed}{price}"
    if model.id not in state["available"]:
        _log(state, "found", model.id, o["name"], detail + "; waiting for a yes on the manager's proposal")
    state["available"][model.id] = {"model": asdict(model), "detail": detail, "at": _now()}


def add_available(model_id: str) -> dict:
    """The owner's Yes on "add this new model"."""
    with _lock:
        state = load()
        found = state["available"].pop(model_id, None)
        if not found:
            raise ValueError("that model isn't waiting to be added")
        state["added"] = [a for a in state["added"] if a["id"] != model_id] + [found["model"]]
        state["blocked"] = [b for b in state["blocked"] if b != model_id]
        _log(state, "added", model_id, found["model"]["display_name"], "approved on the manager's proposal")
        save(state)
        return state


def hide(model_id: str, reason: str) -> dict:
    """The owner's Yes on "remove this old model": out of the pickers (not
    deleted; Restore in the Control Panel brings it back)."""
    model = llm_models._BY_ID.get(model_id)
    if not model:
        raise ValueError("unknown model")
    if model_id in llm_models.in_use():
        raise ValueError("an agent is using this model; switch it first")
    with _lock:
        state = load()
        state["pinned"] = [p for p in state["pinned"] if p != model_id]
        if model_id not in {h["id"] for h in state["hidden"]}:
            _hide(state, model, f"{reason} (approved on the manager's proposal)")
        save(state)
        return state


def _hide(state: dict, model: ModelOption, reason: str) -> None:
    state["hidden"].append({"id": model.id, "reason": reason, "at": _now()})
    _log(state, "hidden", model.id, model.display_name, reason)


def _prune(state: dict, by_route: dict[str, list[dict]], errors: dict) -> None:
    """Notes what the audit may suggest removing (nothing is hidden here):
    models the provider no longer offers, and ones it calls older."""
    rank = {(o["route"], o["api_id"]): o for offered in by_route.values() for o in offered}
    route_ok = lambda route: route in by_route and ("anthropic" if route == "claude-cli" else route) not in errors
    gone, older = [], []
    for m in llm_models.CATALOG:
        if not route_ok(m.provider):
            if m.id in state["gone"]:
                gone.append(m.id)  # couldn't check this time: keep what we knew
            continue
        offer = rank.get((m.provider, m.api_model_id or m.id))
        if offer is None:
            gone.append(m.id)
        elif offer.get("older"):
            older.append(m.id)
    state["gone"], state["provider_older"] = gone, older


def run(probe=_probe) -> dict:
    """One full update. Safe to call from a thread; one run at a time."""
    if not _lock.acquire(blocking=False):
        return load()
    _running.set()
    try:
        state = load()
        by_route, errors = offers()
        for source, err in errors.items():
            _log(state, "error", source, source, f"couldn't read the model list ({err}); nothing changed for it")
        for o in _candidates(state, by_route):
            cid = _catalog_id(o["route"], o["api_id"])
            try:
                seconds = probe(o["route"], o["api_id"])
            except Exception as exc:
                state["failures"][cid] = state["failures"].get(cid, 0) + 1
                _log(state, "failed", cid, o["name"], f"test reply failed ({str(exc)[:140]}); try {state['failures'][cid]} of {MAX_FAILURES}")
                continue
            _offer_found(state, o, seconds)
            save(state)
        _prune(state, by_route, errors)
        state["last_run"] = _now()
        save(state)
        return state
    finally:
        _running.clear()
        _lock.release()


def run_in_background() -> bool:
    if _running.is_set():
        return False
    threading.Thread(target=run, name="openclave-model-updater", daemon=True).start()
    return True


# ── manual review (Control Panel) ─────────────────────────────────────────

def set_enabled(on: bool) -> dict:
    with _lock:
        state = load()
        state["enabled"] = bool(on)
        save(state)
        return state


def remove(model_id: str) -> dict:
    """The user's Remove: hide it now and never add it again."""
    model = llm_models._BY_ID.get(model_id)
    if not model:
        raise ValueError("unknown model")
    if model_id in llm_models.in_use():
        raise ValueError("an agent is using this model; switch it first")
    with _lock:
        state = load()
        state["pinned"] = [p for p in state["pinned"] if p != model_id]
        if model_id not in {h["id"] for h in state["hidden"]}:
            state["hidden"].append({"id": model_id, "reason": "removed by the user", "at": _now()})
        if model_id not in state["blocked"]:
            state["blocked"].append(model_id)
        _log(state, "removed", model_id, model.display_name, "by the user")
        save(state)
        return state


def restore(model_id: str) -> dict:
    """The user's Restore: show it again and keep it from being hidden."""
    model = llm_models._BY_ID.get(model_id)
    if not model:
        raise ValueError("unknown model")
    with _lock:
        state = load()
        state["hidden"] = [h for h in state["hidden"] if h["id"] != model_id]
        state["blocked"] = [b for b in state["blocked"] if b != model_id]
        if model_id not in state["pinned"]:
            state["pinned"].append(model_id)
        _log(state, "restored", model_id, model.display_name, "by the user; won't be hidden automatically")
        save(state)
        return state


def summary() -> dict:
    state = load()
    names = {m.id: m.display_name for m in llm_models.CATALOG}
    return {
        "enabled": state["enabled"], "last_run": state["last_run"], "running": _running.is_set(),
        "log": list(reversed(state["log"][-30:])),
        "hidden": [{**h, "name": names.get(h["id"], h["id"])} for h in state["hidden"]],
        "added": [{"id": a["id"], "name": a["display_name"], "date": a.get("auto_added")} for a in state["added"]],
    }


def _loop() -> None:
    time.sleep(FIRST_RUN_DELAY_S)
    while True:
        try:
            state = load()
            last = datetime.fromisoformat(state["last_run"]) if state["last_run"] else None
            if state["enabled"] and (not last or (datetime.now(timezone.utc) - last).total_seconds() > RUN_EVERY_S):
                run()
        except Exception as exc:
            print(f"model updater: {exc}")
        time.sleep(3600)


def start() -> None:
    threading.Thread(target=_loop, name="openclave-model-updater-loop", daemon=True).start()
