"""Balances and quotas that providers expose with the keys this host has.

Checked 2026-09-24:
- xAI: GET /v1/api-key reports whether the key/team is blocked. The prepaid
  balance needs a separate Management API key, which we don't have.
- Anthropic, OpenAI: no balance endpoint for normal API keys (Anthropic's
  Admin API needs an admin key and reports cost, not balance).
- ElevenLabs: GET /v1/user/subscription reports character quota.
- Fish Audio: GET /wallet/self/api-credit reports credit balance.

Only fetched when the Control Panel asks, in parallel, with short timeouts,
and cached for a few minutes, so it never adds latency to chat.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

TIMEOUT_S = 5
CACHE_S = 300
_cache: dict = {"at": 0.0, "data": None}


def _xai() -> dict:
    r = httpx.get("https://api.x.ai/v1/api-key", timeout=TIMEOUT_S,
                  headers={"Authorization": f"Bearer {os.environ['XAI_API_KEY']}"})
    r.raise_for_status()
    d = r.json()
    blocked = d.get("team_blocked") or d.get("api_key_blocked")
    return {"status": "blocked" if blocked else "active",
            "detail": "Key is blocked" if blocked else "Key active. Balance needs an xAI management key."}


def _elevenlabs() -> dict:
    r = httpx.get("https://api.elevenlabs.io/v1/user/subscription", timeout=TIMEOUT_S,
                  headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]})
    r.raise_for_status()
    d = r.json()
    used, limit = d.get("character_count", 0), d.get("character_limit", 0)
    return {"status": "low" if limit and used / limit >= 0.9 else "ok",
            "used": used, "limit": limit, "detail": f"{used:,} of {limit:,} characters used ({d.get('tier', '?')} plan)"}


def _fish_audio() -> dict:
    r = httpx.get("https://api.fish.audio/wallet/self/api-credit", timeout=TIMEOUT_S,
                  headers={"Authorization": f"Bearer {os.environ['FISH_AUDIO_API_KEY']}"})
    r.raise_for_status()
    credit = float(r.json().get("credit", 0))
    # OpenClave uses Fish Audio's free model (fish_audio_tts.FREE_MODEL), so a
    # zero balance is normal and voices still work.
    return {"status": "ok", "balance": credit,
            "detail": f"${credit:.2f} credit (fine: OpenClave uses the free voice model)"}


CHECKS = {"xAI": _xai, "ElevenLabs (voice)": _elevenlabs, "Fish Audio (voice)": _fish_audio}
NOT_EXPOSED = {
    "Anthropic API": "No balance endpoint for normal API keys; see console.anthropic.com. Out-of-credit errors show in chat.",
    "OpenAI API": "No balance endpoint for API keys.",
}


KEYS = {"xAI": "XAI_API_KEY", "ElevenLabs (voice)": "ELEVENLABS_API_KEY", "Fish Audio (voice)": "FISH_AUDIO_API_KEY"}


def _run(name: str) -> dict:
    key = KEYS.get(name)
    if key and not os.environ.get(key):
        return {"status": "not set up", "detail": f"No {key} (optional)."}
    check = CHECKS[name]
    try:
        return check()
    except Exception as exc:  # a missing key or provider hiccup must not break the panel
        return {"status": "unknown", "detail": f"Could not check ({type(exc).__name__})"}


def get(force: bool = False) -> dict:
    if not force and _cache["data"] is not None and time.monotonic() - _cache["at"] < CACHE_S:
        return _cache["data"]
    with ThreadPoolExecutor(max_workers=len(CHECKS)) as pool:
        results = dict(zip(CHECKS, pool.map(_run, CHECKS)))
    data = {**results, **{name: {"status": "not exposed", "detail": text} for name, text in NOT_EXPOSED.items()}}
    _cache.update(at=time.monotonic(), data=data)
    return data
