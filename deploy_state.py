"""Detects a stale deploy: backend code changed on disk but the service was
never restarted, so the running process still serves the old code while
static files (read fresh from disk) are already new.

Happened 2026-09-23: Codex sessions changed main.py and friends until 19:34,
but openclave.service had been up since 06:31 — the dashboard showed new
buttons whose backend routes 404'd, and nobody noticed for a day.

Fingerprints the top-level, routes/ and providers/ *.py files at import (== process start) and
compares against a fresh fingerprint on demand. Static files are excluded on
purpose: they're served from disk and never need a restart.
"""
import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent


def _py_hashes() -> dict[str, str]:
    files = sorted([*ROOT.glob("*.py"), *ROOT.glob("routes/*.py"), *ROOT.glob("providers/*.py")])
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


STARTED_AT = datetime.now(timezone.utc).isoformat()
_RUNNING_HASHES = _py_hashes()
_RUNNING_COMMIT = _git_commit()


def status() -> dict:
    current = _py_hashes()
    changed = sorted(name for name in current.keys() | _RUNNING_HASHES.keys()
                     if current.get(name) != _RUNNING_HASHES.get(name))
    return {
        "started_at": STARTED_AT,
        "running_commit": _RUNNING_COMMIT,
        "disk_commit": _git_commit(),
        "stale": bool(changed),
        "changed_files": changed,
    }
