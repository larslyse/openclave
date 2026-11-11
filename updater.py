"""Updates: is there a newer OpenClave, and installing it.

One implementation for `openclave update` (bin/openclave) and Control Panel
→ Update. The check is a `git fetch` from the repository OpenClave was
cloned from: besides the model provider it is the only outbound call
OpenClave makes itself, at most once every CHECK_EVERY, in the background,
and only while `[updates] check = true` (the default).

"Newer" means: when the repository has release tags, the latest tag isn't in
this checkout yet; without tags, the upstream branch (origin/main) has
commits this checkout lacks. Installing is `git pull --ff-only` plus the
Python packages; it refuses when the checkout has local changes.

    python3 updater.py check     fetch and say whether a newer version exists
    python3 updater.py apply     pull and install packages (no restart)
    python3 updater.py version   print this checkout's version
"""
import importlib.util
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from state_io import load_json, save_json

ROOT = Path(__file__).resolve().parent
CHECK_EVERY = 6 * 3600
_lock = threading.Lock()
_checking = False


def _git(*args: str, timeout: float = 20) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))


def version() -> dict:
    """This checkout: VERSION file (when there is one) and short commit."""
    try:
        number = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        number = ""
    out = _git("rev-parse", "--short", "HEAD")
    commit = out.stdout.strip() if out.returncode == 0 else ""
    return {"version": number, "commit": commit, "label": number or commit or "unknown"}


def local_changes() -> list[str]:
    """Tracked files changed in this checkout (an update would refuse)."""
    out = _git("status", "--porcelain", "--untracked-files=no")
    return [line[3:] for line in out.stdout.splitlines() if line.strip()] if out.returncode == 0 else []


def _state_file() -> Path | None:
    try:
        from config import CONFIG
        return CONFIG.state_path("update_check.json")
    except Exception:
        return None


def enabled() -> bool:
    try:
        from config import CONFIG
        return CONFIG.update_check
    except Exception:
        return False


def cached() -> dict:
    path = _state_file()
    data = load_json(path, {}) if path else {}
    return data if isinstance(data, dict) else {}


def _save(result: dict) -> None:
    path = _state_file()
    if path:
        save_json(path, result, indent=None)


def check(fetch: bool = True) -> dict:
    """Compare this checkout with upstream. Saves and returns
    {checked_at, available, latest, behind, error}."""
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "available": False,
              "latest": "", "behind": 0, "error": ""}
    if fetch:
        out = _git("fetch", "--quiet", "--tags", timeout=60)
        if out.returncode != 0:
            result["error"] = (out.stderr.strip() or "git fetch failed")[:300]
            _save(result)
            return result
    tags = [t for t in _git("tag", "--list", "--sort=-v:refname").stdout.split() if t]
    if tags:
        latest = tags[0]
        result["latest"] = latest
        result["available"] = _git("merge-base", "--is-ancestor", latest, "HEAD").returncode == 1
    else:
        upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").stdout.strip() or "origin/main"
        count = _git("rev-list", "--count", f"HEAD..{upstream}")
        if count.returncode != 0:
            result["error"] = f"couldn't compare with {upstream}"
        else:
            result["behind"] = int(count.stdout.strip() or 0)
            result["available"] = result["behind"] > 0
            result["latest"] = _git("rev-parse", "--short", upstream).stdout.strip()
    _save(result)
    return result


def check_in_background() -> bool:
    """Start one check unless one is running. Never blocks the caller."""
    global _checking
    with _lock:
        if _checking:
            return False
        _checking = True

    def run():
        global _checking
        try:
            check()
        finally:
            _checking = False

    threading.Thread(target=run, name="update-check", daemon=True).start()
    return True


def is_checking() -> bool:
    return _checking


def _due() -> bool:
    last = cached().get("checked_at")
    try:
        return time.time() - datetime.fromisoformat(last).timestamp() >= CHECK_EVERY
    except (TypeError, ValueError):
        return True


def start() -> None:
    """Background loop: first look 5 minutes after start, then at most once
    every CHECK_EVERY, and only while [updates] check is on."""
    def loop():
        time.sleep(300)
        while True:
            if enabled() and _due():
                check_in_background()
            time.sleep(1800)

    threading.Thread(target=loop, name="update-check-loop", daemon=True).start()


def apply() -> dict:
    """git pull --ff-only, then the Python packages. Returns {ok, old, new, message}."""
    old = version()
    changes = local_changes()
    if changes:
        return {"ok": False, "old": old, "new": old, "message":
                "This OpenClave folder has local changes, so it won't update over them: "
                + ", ".join(changes[:5]) + (" …" if len(changes) > 5 else "")
                + f". Undo or commit them in {ROOT} first."}
    pull = _git("pull", "--ff-only", "--quiet", timeout=180)
    if pull.returncode != 0:
        return {"ok": False, "old": old, "new": old,
                "message": "git pull failed: " + (pull.stderr.strip() or pull.stdout.strip())[:400]}
    steps = [ROOT / "requirements.txt"]
    if importlib.util.find_spec("kokoro_onnx") or importlib.util.find_spec("faster_whisper"):
        steps.append(ROOT / "requirements-voice.txt")
    for req in steps:
        try:
            out = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                                  "-r", str(req)], cwd=ROOT, capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.SubprocessError) as exc:
            out = subprocess.CompletedProcess([], 1, "", str(exc))
        if out.returncode != 0:
            return {"ok": False, "old": old, "new": version(),
                    "message": f"Updated the code, but installing {req.name} failed: "
                               + (out.stderr.strip() or out.stdout.strip())[-400:]}
    new = version()
    check(fetch=False)
    same = new["commit"] == old["commit"]
    return {"ok": True, "old": old, "new": new, "changed": not same,
            "message": "Already up to date." if same else f"Updated from {old['label']} to {new['label']}."}


def _unit() -> str:
    """The systemd unit this process runs in, e.g. "openclave.service", or ""."""
    if not os.environ.get("INVOCATION_ID"):
        return ""
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            name = line.rsplit("/", 1)[-1]
            if name.endswith(".service"):
                return name
    except OSError:
        pass
    return ""


def restart_mode() -> str:
    """How this process can restart itself: "always" (exit cleanly),
    "on-failure" (exit with an error code), or "" (it can't: run by hand)."""
    unit = _unit()
    if not unit:
        return ""
    try:
        out = subprocess.run(["systemctl", "show", unit, "-p", "Restart", "--value"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    policy = out.stdout.strip()
    return policy if policy in {"always", "on-failure"} else ""


def restart_self(mode: str, delay: float = 1.5) -> None:
    """Let the reply go out, then stop so systemd starts the new code."""
    def go():
        if mode == "always":
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            os._exit(75)
    threading.Timer(delay, go).start()


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "version":
        v = version()
        print(f"{v['version']} (commit {v['commit']})" if v["version"] else v["label"])
        return 0
    if cmd == "check":
        r = check()
        if r["error"]:
            print(f"Couldn't check: {r['error']}")
            return 1
        print(f"A newer version is available: {r['latest']}." if r["available"] else "You have the latest version.")
        return 0
    if cmd == "apply":
        r = apply()
        print(r["message"])
        return 0 if r["ok"] and r.get("changed") else (4 if r["ok"] else 1)
    print(__doc__.strip().split("\n\n")[-1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
