"""Long-lived STT/TTS worker, run under the configured voice interpreter
because that's where faster-whisper/kokoro-onnx and the configured model weights
are installed. This keeps voice dependencies separate from the service
environment.

Talks newline-delimited JSON over stdin/stdout. One request in flight at a
time (voice_engine.py serializes calls) — simple beats a real RPC framework
at this scale. Spawned once by voice_engine.start() and kept alive for the
life of the openclave.service process specifically so both models stay warm:
faster-whisper's cold load alone measured ~8s (see voice.md), which would
defeat the point of a fast conversational voice path if paid on every turn.
"""
import json
import sys
import config

DEFAULT_MODELS_DIR = str(config.CONFIG.voice_model_dir) if config.CONFIG.voice_model_dir else None


def main(models_dir: str | None = None):
    if not models_dir:
        raise RuntimeError("voice disabled: no voice model directory configured")
    from faster_whisper import WhisperModel
    from kokoro_onnx import Kokoro
    import soundfile as sf

    whisper = WhisperModel("small", device="cpu", compute_type="int8")
    kokoro = Kokoro(f"{models_dir}/kokoro-v1.0.onnx", f"{models_dir}/voices-v1.0.bin")
    print(json.dumps({"ready": True}), flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        try:
            if req["op"] == "transcribe":
                # Always a fixed language: whisper-small's auto-detect heard
                # native Norwegian as English and translated it.
                # vad_filter drops non-speech first: on room noise or a click,
                # whisper otherwise invents "You"/"Thank you" (reproduced;
                # Auto would send those as messages). Segments whisper
                # itself thinks are probably not speech are dropped too.
                segments, _info = whisper.transcribe(req["path"], language=req.get("language", "en"), vad_filter=True)
                text = " ".join(seg.text.strip() for seg in segments
                                if not (seg.no_speech_prob > 0.6 and seg.avg_logprob < -0.7))
                resp = {"id": req["id"], "ok": True, "text": text}
            elif req["op"] == "synthesize":
                samples, sr = kokoro.create(
                    req["text"], voice=req.get("voice", "af_heart"), speed=1.0, lang="en-us"
                )
                sf.write(req["path"], samples, sr)
                resp = {"id": req["id"], "ok": True, "duration_s": len(samples) / sr}
            else:
                resp = {"id": req["id"], "ok": False, "error": f"unknown op: {req['op']}"}
        except Exception as exc:
            resp = {"id": req.get("id"), "ok": False, "error": str(exc)[:500]}
        print(json.dumps(resp), flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODELS_DIR)
