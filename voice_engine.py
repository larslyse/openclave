"""Manages voice_worker.py as a long-lived child process of openclave.service
(started at FastAPI startup, killed at shutdown) — not a separate systemd
unit. Keeping it a plain child process avoids installing a second service on
this box and keeps its lifecycle tied to the service that actually needs it.

Not thread-safe across concurrent transcribe/synthesize calls by design: one
worker, one stdin/stdout pipe, a lock serializes callers. Voice is low
concurrency (the owner talks to one agent at a time) so this is not a real
bottleneck; it mirrors chat_service's existing per-agent execution lock in
spirit.
"""
import json
from pathlib import Path
import subprocess
import threading
import time
import config

KIWI_PYTHON = config.CONFIG.voice_python
VOICE_MODEL_DIR = config.CONFIG.voice_model_dir
WORKER_SCRIPT = str(Path(__file__).parent / "voice_worker.py")
STARTUP_TIMEOUT_S = 30
CALL_TIMEOUT_S = 60

_proc: subprocess.Popen | None = None
_lock = threading.Lock()
_next_id = 0


class VoiceEngineError(RuntimeError):
    pass


def _spawn() -> subprocess.Popen:
    if not available():
        raise VoiceEngineError("voice disabled: configure [voice].python and [voice].model_dir")
    proc = subprocess.Popen(
        [KIWI_PYTHON, WORKER_SCRIPT, str(VOICE_MODEL_DIR)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    line = proc.stdout.readline()
    while not line and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
        line = proc.stdout.readline()
    if not line:
        stderr = proc.stderr.read(2000) if proc.stderr else ""
        proc.kill()
        raise VoiceEngineError(f"voice worker failed to start: {stderr}")
    ready = json.loads(line)
    if not ready.get("ready"):
        raise VoiceEngineError(f"voice worker sent unexpected startup line: {line!r}")
    return proc


def available() -> bool:
    """Whether the local worker has both required settings configured."""
    return bool(KIWI_PYTHON and VOICE_MODEL_DIR is not None)


def start() -> None:
    global _proc
    if not available():
        return
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return
        _proc = _spawn()


def stop() -> None:
    global _proc
    with _lock:
        if _proc is not None:
            _proc.terminate()
            try:
                _proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _proc.kill()
            _proc = None


def _call(op: str, **fields) -> dict:
    global _proc, _next_id
    with _lock:
        if _proc is None or _proc.poll() is not None:
            _proc = _spawn()
        _next_id += 1
        req_id = _next_id
        payload = json.dumps({"id": req_id, "op": op, **fields})
        try:
            _proc.stdin.write(payload + "\n")
            _proc.stdin.flush()
            line = _proc.stdout.readline()
        except (BrokenPipeError, OSError) as exc:
            raise VoiceEngineError(f"voice worker pipe broke: {exc}") from exc
        if not line:
            stderr = _proc.stderr.read(2000) if _proc.stderr else ""
            raise VoiceEngineError(f"voice worker produced no response (stderr: {stderr})")
        resp = json.loads(line)
        if resp.get("id") != req_id:
            raise VoiceEngineError(f"voice worker response id mismatch: expected {req_id}, got {resp.get('id')}")
        if not resp.get("ok"):
            raise VoiceEngineError(resp.get("error", "voice worker call failed"))
        return resp


def transcribe(audio_path: str, language: str = "en") -> str:
    return _call("transcribe", path=audio_path, language=language)["text"]


def synthesize(text: str, out_path: str, voice: str = "af_heart") -> float:
    return _call("synthesize", text=text, path=out_path, voice=voice)["duration_s"]
