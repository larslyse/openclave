"""Each agent's own voice for voice mode (2026-10-12: "it should be a
part of onboarding to have custom voice on each agent", Fish Audio for now).

The agent finds candidates in Fish Audio's public voice library (find_voice),
The owner listens to the sample links and picks one, and the agent saves it
(set_voice). Stored in the agent's agent.json as
{"voice": {"provider": "fish_audio", "voice_id": ..., "name": ...}}.
Agents without one use the local default voice. Norwegian replies use the
Norwegian voice default, with an optional per-agent override.
"""
import json
import os
import re

import requests

import agents

FISH_MODELS_URL = "https://api.fish.audio/model"
TIMEOUT_S = 15
_VOICE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class VoiceError(RuntimeError):
    pass


def _headers() -> dict:
    key = os.environ.get("FISH_AUDIO_API_KEY")
    if not key:
        raise VoiceError("FISH_AUDIO_API_KEY not set, so Fish Audio voices can't be searched")
    return {"Authorization": f"Bearer {key}"}


def _summary(m: dict) -> dict:
    samples = [s.get("audio") for s in m.get("samples") or [] if s.get("audio")]
    return {
        "id": m.get("_id"),
        "name": m.get("title", ""),
        "description": " ".join(str(m.get("description") or "").split())[:160],
        "tags": [t for t in (m.get("tags") or [])][:6],
        "languages": m.get("languages") or [],
        "sample": samples[0] if samples else None,
    }


def search(query: str, language: str | None = "en", limit: int = 5) -> list[dict]:
    params = {"page_size": max(1, min(int(limit or 5), 10)), "title": query.strip()[:80],
              "sort_by": "score"}
    if language:
        params["language"] = language
    try:
        r = requests.get(FISH_MODELS_URL, headers=_headers(), params=params, timeout=TIMEOUT_S)
    except requests.RequestException as exc:
        raise VoiceError(f"Fish Audio search failed: {exc}") from exc
    if r.status_code != 200:
        raise VoiceError(f"Fish Audio search failed ({r.status_code})")
    items = [m for m in r.json().get("items", []) if m.get("type", "tts") == "tts"]
    return [_summary(m) for m in items]


def lookup(voice_id: str) -> dict:
    if not _VOICE_ID_RE.fullmatch(voice_id or ""):
        raise VoiceError("that isn't a Fish Audio voice id (32 hex characters, from find_voice)")
    try:
        r = requests.get(f"{FISH_MODELS_URL}/{voice_id}", headers=_headers(), timeout=TIMEOUT_S)
    except requests.RequestException as exc:
        raise VoiceError(f"Fish Audio lookup failed: {exc}") from exc
    if r.status_code != 200:
        raise VoiceError(f"no Fish Audio voice {voice_id} ({r.status_code})")
    return _summary(r.json())


def _agent_file(agent_id: str):
    return agents.AGENTS_ROOT / agents.AGENTS[agent_id].folder / agents.AGENT_FILE


def get(agent_id: str) -> dict | None:
    """The agent's own voice from agent.json, or None."""
    if agent_id not in agents.AGENTS:
        return None
    try:
        voice = json.loads(_agent_file(agent_id).read_text(encoding="utf-8")).get("voice")
    except (OSError, ValueError):
        return None
    if isinstance(voice, dict) and voice.get("provider") == "fish_audio" and _VOICE_ID_RE.fullmatch(str(voice.get("voice_id", ""))):
        return voice
    return None


def set_voice(agent_id: str, voice_id: str) -> dict:
    """Checks the voice exists, then saves it in agent.json."""
    found = lookup(voice_id.strip())
    f = _agent_file(agent_id)
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"id": agent_id}
    data["voice"] = {"provider": "fish_audio", "voice_id": found["id"], "name": found["name"]}
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(f)
    return data["voice"]
