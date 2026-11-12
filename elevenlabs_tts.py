"""ElevenLabs text-to-speech — a per-agent alternative to the local Kokoro
path (voice_engine.py) for agents where character accuracy matters more than
staying fully local/free. Used for a requested voice (Rick's) that Kokoro's small stock voice bank
has nothing close to. See Build Docs/Archive/voice.md's provider comparison.
"""
import os

import requests

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"
# Flash model line: ElevenLabs' lowest-latency tier (~75ms model inference for
# short inputs per their own latency docs) — chosen deliberately over the
# eleven_multilingual_v2 default, since voice.md flags latency/expressiveness
# as a real tradeoff to pick, not default into.
FLASH_MODEL_ID = "eleven_flash_v2_5"
TIMEOUT_S = 30


class ElevenLabsError(RuntimeError):
    pass


def synthesize(text: str, voice_id: str, out_path: str, *, model_id: str = FLASH_MODEL_ID) -> None:
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise ElevenLabsError("ELEVENLABS_API_KEY not set")
    try:
        response = requests.post(
            f"{ELEVENLABS_BASE_URL}/text-to-speech/{voice_id}",
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={"text": text, "model_id": model_id},
            timeout=TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise ElevenLabsError(f"ElevenLabs request failed: {exc}") from exc
    if response.status_code != 200:
        raise ElevenLabsError(f"ElevenLabs TTS failed ({response.status_code}): {response.text[:300]}")
    with open(out_path, "wb") as f:
        f.write(response.content)
