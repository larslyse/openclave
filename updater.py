"""Updates: is there a newer OpenClave, and installing it.

One implementation for install.sh, `openclave update` (bin/openclave) and
Control Panel → Update. The check is a `git fetch` from the repository
OpenClave was cloned from: besides the model provider it is the only outbound
call OpenClave makes itself, at most once every CHECK_EVERY, in the
background, and only while `[updates] check = true` (the default).

The channel rule (`[updates] channel` in openclave.toml):

- "release" (the default): the checkout sits on the newest `vX.Y.Z` tag, as
  a detached HEAD. While the repository has no such tag yet, it follows its
  branch (the upstream, e.g. origin/main) as before.
- "main": follow the branch (its upstream, or origin's default branch when
  the checkout sits on a tag), for developers.

Updates only ever move forward: the target must contain the current commit.
A checkout that is ahead of the newest release (e.g. an older install that
followed main) stays where it is until a newer release comes out. Installing
refuses when tracked files have local changes.

    python3 updater.py check                fetch and say whether a newer version exists
    python3 updater.py apply                move forward and install packages (no restart)
    python3 updater.py apply --code-only    move forward only (install.sh installs packages)
    python3 updater.py first-checkout       after a fresh clone: go to the channel's target
    python3 updater.py version              print this checkout's version
"""
import importlib.util
import os
import re
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
FIRST_LOOK_S = 300   # first look after startup
LOOK_EVERY_S = 1800  # how often the scheduler asks whether a check is due
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
_lock = threading.Lock()
_checking = False


def _git(*args: str, timeout: float = 20) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))


def version() -> dict:
    """This checkout: VERSION file (when there is one), short commit, and the
    release tag when HEAD sits exactly on one."""
    try:
        number = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        number = ""
    out = _git("rev-parse", "--short", "HEAD")
    commit = out.stdout.strip() if out.returncode == 0 else ""
    tags = [t for t in _git("tag", "--points-at", "HEAD").stdout.split() if TAG_RE.match(t)]
    return {"version": number, "commit": commit, "tag": tags[0] if tags else "",
            "label": number or commit or "unknown"}


def local_changes() -> list[str]:
    """Tracked files changed in this checkout (an update would refuse)."""
    out = _git("status", "--porcelain", "--untracked-files=no")
    return [line[3:] for line in out.stdout.splitlines() if line.strip()] if out.returncode == 0 else []


def channel() -> str:
    """[updates] channel: "release" (default) or "main"."""
    try:
        from config import CONFIG
        return CONFIG.update_channel
    except Exception:
        return "release"


def newest_tag() -> str:
    """The newest release tag (vX.Y.Z, by version order), or ""."""
    for tag in _git("tag", "--list", "v*", "--sort=-v:refname").stdout.split():
        if TAG_RE.match(tag):
            return tag
    return ""


def _commit(ref: str) -> str:
    out = _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.stdout.strip() if out.returncode == 0 else ""


def _contains(older: str, newer: str) -> bool:
    """True when `newer` already has every commit of `older` (or is it)."""
    return _git("merge-base", "--is-ancestor", older, newer).returncode == 0


def _branch_target() -> tuple[str, str, str]:
    """(local branch, remote ref, error) to follow: the current branch's
    upstream, or origin's default branch on a detached HEAD."""
    branch = _git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    if branch:
        upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").stdout.strip()
        if not upstream:
            return branch, "", f"the branch {branch} has no upstream branch to follow"
        return branch, upstream, ""
    default = _git("symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD").stdout.strip() or "origin/main"
    return default.split("/", 1)[-1], default, ""


def plan(chan: str | None = None, fresh: bool = False) -> dict:
    """Where an update would move this checkout, by the channel rule (no
    fetch, nothing changed). `fresh` (a new clone, nothing installed yet) may
    go back to the newest release; otherwise updates only move forward.
    Returns {channel, kind ("tag"/"branch"), target, branch, latest,
    latest_version, behind, available, ahead, summary, error}."""
    chan = chan or channel()
    result = {"channel": chan, "kind": "", "target": "", "branch": "", "latest": "", "latest_version": "",
              "behind": 0, "available": False, "ahead": False, "summary": "", "error": ""}
    tag = newest_tag() if chan == "release" else ""
    if tag:
        result.update(kind="tag", target=tag, latest=tag)
    else:
        branch, ref, error = _branch_target()
        if error or not _commit(ref):
            result["error"] = error or f"couldn't find {ref}"
            return result
        result.update(kind="branch", target=ref, branch=branch,
                      latest=_git("rev-parse", "--short", ref).stdout.strip())
    shown = _git("show", f"{result['target']}:VERSION")
    result["latest_version"] = shown.stdout.strip() if shown.returncode == 0 else ""
    head, target = _commit("HEAD"), _commit(result["target"])
    if target == head:
        result["summary"] = f"You have the latest release ({tag})." if tag else "You have the latest version."
        return result
    if not (fresh and tag) and not _contains(head, target):
        result["ahead"] = True
        result["summary"] = (f"This copy is ahead of the latest release ({tag}), so it stays as it is "
                             "until a newer release comes out." if tag else
                             f"This copy has commits that {result['target']} doesn't have, "
                             "so it won't update over them.")
        return result
    count = _git("rev-list", "--count", f"HEAD..{result['target']}")
    result["behind"] = int(count.stdout.strip() or 0) if count.returncode == 0 else 0
    result["available"] = True
    if tag:
        number = f" (version {result['latest_version']})" if result["latest_version"] else ""
        result["summary"] = f"A newer release is available: {tag}{number}."
    else:
        n = result["behind"]
        result["summary"] = f"{n} new change{'' if n == 1 else 's'} on {result['target']}."
    return result


def move(p: dict) -> tuple[bool, str]:
    """Check out what plan() found. Returns (ok, error)."""
    if not p["available"]:
        return True, ""
    if p["kind"] == "tag":
        out = _git("-c", "advice.detachedHead=false", "checkout", "-q", "--detach", p["target"], timeout=120)
    else:
        branch, ref = p["branch"], p["target"]
        if _git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip() == branch:
            out = _git("merge", "--ff-only", "-q", ref, timeout=120)
        else:  # on a release tag: back onto the branch
            if _commit(branch) and not _contains(branch, ref):
                return False, f"the local branch {branch} has commits that {ref} doesn't have"
            out = _git("checkout", "-q", "-B", branch, "--track", ref, timeout=120)
    if out.returncode != 0:
        return False, (out.stderr.strip() or out.stdout.strip() or "git checkout failed")[:400]
    return True, ""


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


def _fetch(timeout: float) -> subprocess.CompletedProcess:
    return _git("fetch", "--quiet", "--tags", "origin", timeout=timeout)


def check(fetch: bool = True) -> dict:
    """Compare this checkout with upstream by the channel rule. Saves and
    returns {checked_at, current, channel, available, latest, latest_version,
    behind, ahead, summary, error}."""
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "current": version(),
              "channel": channel(), "available": False, "latest": "", "latest_version": "",
              "behind": 0, "ahead": False, "summary": "", "error": ""}
    if fetch:
        out = _fetch(60)
        if out.returncode != 0:
            result["error"] = (out.stderr.strip() or "git fetch failed")[:300]
            _save(result)
            return result
    p = plan(result["channel"])
    for key in ("available", "latest", "latest_version", "behind", "ahead", "summary", "error"):
        result[key] = p[key]
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


def run_due_once() -> bool:
    """Checks for an update when [updates] check is on and the last check is
    CHECK_EVERY old. Skips while a check started from the Control Panel runs."""
    global _checking
    if not (enabled() and _due()):
        return False
    with _lock:
        if _checking:
            return False
        _checking = True
    try:
        check()
    finally:
        _checking = False
    return True


def start() -> None:
    """Registers the update check with the scheduler: first look
    FIRST_LOOK_S after startup, then every LOOK_EVERY_S, so a check happens
    at most once every CHECK_EVERY. The fetch runs in the worker pool."""
    import scheduler
    scheduler.register("update-check", "Check for an OpenClave update", run_due_once,
                       every=LOOK_EVERY_S, delay=FIRST_LOOK_S, worker=True)


def _label(v: dict) -> str:
    return f"{v['label']} (commit {v['commit']})" if v["version"] and v["commit"] else v["label"]


def apply(packages: bool = True) -> dict:
    """Fetch, move forward by the channel rule, then the Python packages
    (unless packages=False). Returns {ok, old, new, changed, message}."""
    old = version()
    changes = local_changes()
    if changes:
        return {"ok": False, "old": old, "new": old, "message":
                "This OpenClave folder has local changes, so it won't update over them: "
                + ", ".join(changes[:5]) + (" …" if len(changes) > 5 else "")
                + f". Undo or commit them in {ROOT} first."}
    fetch = _fetch(180)
    if fetch.returncode != 0:
        return {"ok": False, "old": old, "new": old,
                "message": "git fetch failed: " + (fetch.stderr.strip() or fetch.stdout.strip())[:400]}
    p = plan()
    moved, error = (False, p["error"]) if p["error"] else move(p)
    if not moved:
        return {"ok": False, "old": old, "new": old, "message": f"Couldn't update: {error}."}
    steps = []
    if packages:
        steps.append(ROOT / "requirements.txt")
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
    if same:
        message = p["summary"] if p["ahead"] else "Already up to date."
    else:
        message = f"Updated from {_label(old)} to {_label(new)}."
    return {"ok": True, "old": old, "new": new, "changed": not same, "message": message}


def first_checkout() -> dict:
    """Right after a fresh `git clone` (install.sh): on the release channel go
    to the newest release, although the clone starts on the newer main."""
    p = plan(fresh=True)
    moved, error = (False, p["error"]) if p["error"] else move(p)
    if not moved:
        return {"ok": False, "message": f"Couldn't pick the version: {error}."}
    if p["kind"] == "tag":
        return {"ok": True, "message": f"Using release {p['target']}."}
    return {"ok": True, "message": f"Using the latest {p['branch'] or 'main'} (no release yet)."}


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
        print(f"{r['summary']} (channel: {r['channel']})")
        return 0
    if cmd == "apply":
        r = apply(packages="--code-only" not in argv[1:])
        print(r["message"])
        return 0 if r["ok"] and r.get("changed") else (4 if r["ok"] else 1)
    if cmd == "first-checkout":
        r = first_checkout()
        print(r["message"])
        return 0 if r["ok"] else 1
    print(__doc__.strip().split("\n\n")[-1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
