"""Fish Audio text-to-speech — Rick's and Rocky's real voices, pre-built
community voice models (not designed/cloned by us). Verified live 2026-09-21:
real API calls, ~2.5-3s for a short reply, genuinely usable free tier
(s2.1-pro-free), no CPU/GPU bottleneck since it's cloud-hosted. See Build
Docs/voice.md for the comparison against ElevenLabs (blocked by a paid-tier
library-voice restriction) and Chatterbox (too slow on a home server's CPU, ~85s for
one reply).
"""
import os

import requests

FISH_AUDIO_URL = "https://api.fish.audio/v1/tts"
# The genuinely free tier, not a trial of a paid one — confirmed live.
FREE_MODEL = "s2.1-pro-free"
TIMEOUT_S = 30


class FishAudioError(RuntimeError):
    pass


def synthesize(text: str, reference_id: str, out_path: str, *, model: str = FREE_MODEL) -> None:
    api_key = os.environ.get("FISH_AUDIO_API_KEY")
    if not api_key:
        raise FishAudioError("FISH_AUDIO_API_KEY not set")
    try:
        response = requests.post(
            FISH_AUDIO_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "model": model,
            },
            json={"text": text, "reference_id": reference_id, "format": "mp3", "mp3_bitrate": 128},
            timeout=TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise FishAudioError(f"Fish Audio request failed: {exc}") from exc
    if response.status_code != 200:
        raise FishAudioError(f"Fish Audio TTS failed ({response.status_code}): {response.text[:300]}")
    with open(out_path, "wb") as f:
        f.write(response.content)
