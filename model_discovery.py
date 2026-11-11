"""Which models the providers offer that OpenClave's catalog doesn't have yet.

Report-only: the full list of what providers offer beyond the pickers, older
models included. model_updater.py is what adds the newer ones (after a test
reply) and hides old ones.
Sources, all read-only: xAI GET /v1/models, Anthropic Models API (covers the
API and the Claude CLI), and `codex debug models` (the ChatGPT subscription).
The result is cached in OPENCLAVE_HOME/state/model_check.json and refreshed when the
Control Panel is opened and the last check is over a day old.
"""
import json
import os
import re
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from config import CONFIG
from state_io import load_json, save_json
import llm_client
import llm_models

CHECK_FILE = CONFIG.state_path("model_check.json")
MAX_AGE = timedelta(days=1)
_refresh_lock = threading.Lock()

# Not chat models, or not usable here.
_SKIP = re.compile(r"imagine|image|video|multi-agent|embedding|reserve|auto-review")
# Anthropic IDs with a date suffix are previous generations kept for pinning.
_DATED = re.compile(r"-\d{8}$")


def _known_ids() -> set[str]:
    return {m.api_model_id or m.id for m in llm_models.CATALOG} | {m.id for m in llm_models.CATALOG}


def _xai() -> list[str]:
    r = httpx.get("https://api.x.ai/v1/models", timeout=10,
                  headers={"Authorization": f"Bearer {os.environ['XAI_API_KEY']}"})
    r.raise_for_status()
    return [m["id"] for m in r.json()["data"]]


def _anthropic() -> list[str]:
    import anthropic
    return [m.id for m in anthropic.Anthropic().models.list(limit=100) if not _DATED.search(m.id)]


def _codex() -> list[str]:
    out = subprocess.run([llm_client.CODEX_BIN, "debug", "models"], capture_output=True, text=True, timeout=60)
    return [m["slug"] for m in json.loads(out.stdout)["models"] if m.get("visibility") == "list"]


SOURCES = {"xAI API": _xai, "Anthropic (API and Claude CLI)": _anthropic, "Codex (ChatGPT subscription)": _codex}


def check() -> dict:
    known = _known_ids()
    providers = {}
    for name, fetch in SOURCES.items():
        try:
            offered = [m for m in fetch() if not _SKIP.search(m)]
            providers[name] = {"new": [m for m in offered if m not in known], "error": None}
        except Exception as exc:
            providers[name] = {"new": [], "error": f"could not check ({type(exc).__name__})"}
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "providers": providers}
    save_json(CHECK_FILE, result)
    return result


def cached() -> dict | None:
    return load_json(CHECK_FILE, None)


def is_stale(result: dict | None) -> bool:
    if not result:
        return True
    return datetime.now(timezone.utc) - datetime.fromisoformat(result["checked_at"]) > MAX_AGE


def refresh_in_background() -> bool:
    """Start one background check unless one is already running."""
    if not _refresh_lock.acquire(blocking=False):
        return False

    def run():
        try:
            check()
        finally:
            _refresh_lock.release()
    threading.Thread(target=run, daemon=True).start()
    return True
