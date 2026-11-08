"""Claude CLI sign-in (Rick's backlog, 2026-10-14).

The claude-cli route runs on the user's Claude subscription login
(~/.claude/.credentials.json). When that login expires every claude-cli
agent failed with a bare "(claude-cli error: ...)". Now:

  - llm_client spots the CLI's own auth failure (an assistant event with
    error "authentication_failed", or "Please run /login") and calls
    mark_signed_out(); the agent's reply says what happened and where to fix
    it, and attention.py puts the amber dot on agents on a Claude CLI model.
  - Control Panel → Claude sign-in runs `claude auth login` on the OpenClave computer.
    That prints a sign-in URL and then waits for a code: the user opens the link,
    signs in, and pastes the code the page shows back into the Control Panel,
    which writes it to the process. Then one tiny `claude -p` call proves
    access is back before it says so.

Nothing here reads or returns the credentials file or the CLI's raw output:
the UI gets the sign-in URL (PKCE, no secret in it) and fixed status texts.
"""
import json
import os
import re
import select
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from config import CONFIG

STATE_FILE = CONFIG.state_path("claude_auth.json")
LOGIN_URL_TIMEOUT_S = 30
LOGIN_CODE_TIMEOUT_S = 90
LOGIN_IDLE_TIMEOUT_S = 15 * 60  # an unused sign-in process is stopped
VERIFY_TIMEOUT_S = 120
VERIFY_PROMPT = "Reply with just: ok"

_AUTH_TEXT_RE = re.compile(
    r"(?i)(not logged in|please run /login|oauth token (has )?expired|"
    r"invalid (api key|bearer token)|authentication_error|authentication_failed|"
    r"token (has been )?revoked)")
_URL_RE = re.compile(r"https://[^\s\"'<>]+/oauth/authorize\?[^\s\"'<>]+")
_CODE_RE = re.compile(r"^[A-Za-z0-9_\-#.~]{8,512}$")

_lock = threading.Lock()
_login: dict = {"state": "idle"}  # idle | waiting_code | checking | done | failed
_proc: subprocess.Popen | None = None
_buffer = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bin() -> str:
    import llm_client  # lazy: llm_client imports this module
    return llm_client.CLAUDE_BIN


def _env() -> dict:
    """The service's env minus the Console API key (it would win over the
    subscription login, see llm_client) and any way to open a browser here."""
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "DISPLAY")}
    env["BROWSER"] = "/bin/false"
    return env


# ── Detection ────────────────────────────────────────────────────────────

def is_auth_failure(output: str, reason: str = "") -> bool:
    """True when Claude CLI's stream-json output says it isn't signed in."""
    for line in (output or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("error") == "authentication_failed":
            return True
    return bool(reason and _AUTH_TEXT_RE.search(reason))


def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def signed_out() -> bool:
    return bool(_load().get("signed_out"))


def mark_signed_out(reason: str = "") -> None:
    state = _load()
    if not state.get("signed_out"):
        state.update(signed_out=True, since=_now())
    state["reason"] = (reason or "")[:200]
    _save(state)


def mark_ok() -> None:
    """A claude-cli call worked. Cheap when nothing was wrong (no write)."""
    state = _load()
    if state.get("signed_out"):
        _save({"signed_out": False, "restored": _now()})


AGENT_REPLY = ("(claude-cli error: Claude CLI is signed out, so this agent can't answer on its Claude CLI model. "
               "Sign in again in the Control Panel → Claude sign-in (/control/#claude-login), "
               "or switch this agent to another model for now.)")


# ── Sign-in flow ─────────────────────────────────────────────────────────

def _read_output(proc: subprocess.Popen) -> None:
    """Collect the login process's output (its prompt has no newline, so read
    raw bytes rather than lines). Kept here only to find the URL and the
    outcome; never sent to the UI."""
    global _buffer
    fd = proc.stdout.fileno()
    while True:
        try:
            ready, _, _ = select.select([fd], [], [], 0.5)
        except (OSError, ValueError):
            return
        if ready:
            chunk = os.read(fd, 4096)
            if not chunk:
                return
            with _lock:
                _buffer = (_buffer + chunk.decode("utf-8", "replace"))[-20000:]
        elif proc.poll() is not None:
            return


def _stop_proc() -> None:
    global _proc
    proc, _proc = _proc, None
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)


def _set(**fields) -> None:
    global _login
    _login = {**fields, "updated": _now()}


def start_login() -> dict:
    """Start `claude auth login` and return its sign-in URL. A sign-in that's
    already waiting for its code is reused (same URL), so a double tap or a
    second screen doesn't invalidate the first link."""
    global _proc, _buffer
    with _lock:
        if _login.get("state") == "waiting_code" and _proc and _proc.poll() is None:
            return status()
    _stop_proc()
    with _lock:
        _buffer = ""
    _proc = subprocess.Popen([_bin(), "auth", "login", "--claudeai"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=_env(),
                             cwd=str(Path.home()), start_new_session=True)
    threading.Thread(target=_read_output, args=(_proc,), name="claude-login", daemon=True).start()
    deadline = time.monotonic() + LOGIN_URL_TIMEOUT_S
    url = None
    while time.monotonic() < deadline and _proc.poll() is None:
        with _lock:
            m = _URL_RE.search(_buffer)
        if m:
            url = m.group(0)
            break
        time.sleep(0.2)
    if not url:
        _stop_proc()
        _set(state="failed", message="Claude CLI didn't give a sign-in link. Sign in from a terminal instead: open a terminal on the OpenClave computer, run `claude` as the user OpenClave runs as, then /login.")
        return status()
    _set(state="waiting_code", url=url, started=time.monotonic())
    proc = _proc
    timer = threading.Timer(LOGIN_IDLE_TIMEOUT_S, _expire, args=(proc,))
    timer.daemon = True
    timer.start()
    return status()


def _expire(proc) -> None:
    if _proc is proc and _login.get("state") == "waiting_code":
        _stop_proc()
        _set(state="failed", message="The sign-in link expired. Start again.")


def submit_code(code: str) -> dict:
    """Give the login process the code from the sign-in page. Finishing and
    the access check run in the background; poll status()."""
    code = (code or "").strip()
    if not _CODE_RE.match(code):
        raise ValueError("That doesn't look like a sign-in code. Copy the whole code from the page.")
    proc = _proc
    if _login.get("state") != "waiting_code" or not proc or proc.poll() is not None:
        raise LookupError("No sign-in is waiting for a code. Start again.")
    with _lock:
        mark = len(_buffer)
    try:
        proc.stdin.write((code + "\n").encode())
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        _set(state="failed", message="The sign-in process stopped. Start again.")
        return status()
    _set(state="checking", message="Signing in…")
    threading.Thread(target=_finish, args=(proc, mark), name="claude-login-finish", daemon=True).start()
    return status()


def _finish(proc: subprocess.Popen, mark: int) -> None:
    deadline = time.monotonic() + LOGIN_CODE_TIMEOUT_S
    while time.monotonic() < deadline and proc.poll() is None:
        with _lock:
            after = _buffer[mark:]
        if re.search(r"(?i)invalid code|error|failed", after):
            break
        time.sleep(0.3)
    with _lock:
        after = _buffer[mark:]
    if proc.poll() != 0:
        _stop_proc()
        wrong = re.search(r"(?i)invalid code", after)
        _set(state="failed", message="Claude didn't accept that code. Start again and paste the whole code." if wrong
             else "Sign-in didn't finish. Start again.")
        return
    _set(state="checking", message="Signed in. Checking that agents can use it…")
    ok, detail = verify()
    if ok:
        mark_ok()
        _set(state="done", message="Access restored: Claude CLI answered a test message.")
    else:
        _set(state="failed", message=f"Signed in, but the test message failed: {detail}")


def verify() -> tuple[bool, str]:
    """One tiny real call, the same way agents call it. Returns (ok, short reason)."""
    try:
        res = subprocess.run([_bin(), "-p", VERIFY_PROMPT, "--model", "haiku", "--output-format", "stream-json",
                              "--verbose", "--setting-sources", ""],
                             capture_output=True, text=True, env=_env(), cwd=str(Path.home()),
                             timeout=VERIFY_TIMEOUT_S, start_new_session=True)
    except subprocess.TimeoutExpired:
        return False, "no answer within 2 minutes"
    except OSError as exc:
        return False, str(exc)[:200]
    if is_auth_failure(res.stdout):
        mark_signed_out("still signed out after sign-in")
        return False, "Claude CLI still says it isn't signed in"
    if res.returncode != 0:
        import llm_client
        return False, (llm_client._claude_stream_error(res.stdout) or f"exit code {res.returncode}")[:200]
    return True, ""


def cancel() -> dict:
    _stop_proc()
    _set(state="idle")
    return status()


def status() -> dict:
    state = _load()
    login = {k: v for k, v in _login.items() if k in ("state", "url", "message", "updated")}
    return {"signed_out": bool(state.get("signed_out")), "since": state.get("since"),
            "restored": state.get("restored"), "login": login}
