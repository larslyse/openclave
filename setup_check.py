"""What is set up on this computer: which model routes can answer, and the
optional extras. One source for the home page's first-run card, the Control
Panel's Setup section, the model pickers' "not set up" rows and
`python3 run.py --check`.

The check itself uses only local facts (an environment variable is set, a
program is on PATH, the Claude CLI has a login), so it is instant and never
calls a provider. A key that is set but wrong shows up as an error in the
first chat instead. The one exception is save_key: a key pasted in the
Control Panel is tried against its provider once before it is saved.
"""
import os
import re
import shutil
import sys
from pathlib import Path

import httpx

from config import CONFIG
from version import VERSION

# The model routes, in the order a new user is pointed to them.
PROVIDERS = ("anthropic", "xai", "claude-cli", "codex-cli")


def _which(program: str) -> str | None:
    return shutil.which(os.path.expanduser(program))


def _env_file() -> str:
    return str(CONFIG.home / ".env")


def _claude_signed_in() -> bool:
    """The Claude CLI has a login: its credentials file on Linux (macOS keeps
    it in the Keychain, so there only a failed call says otherwise), and no
    failed call since (claude_auth)."""
    import claude_auth
    if claude_auth.signed_out():
        return False
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or sys.platform == "darwin":
        return True
    config_dir = Path(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))
    return (config_dir / ".credentials.json").is_file()


def routes() -> list[dict]:
    """One row per model route: id (= llm_models provider), name, ready, detail, fix."""
    claude, codex = _which(CONFIG.claude_bin), _which(CONFIG.codex_bin)
    claude_in = bool(claude) and _claude_signed_in()
    needs_bwrap = sys.platform.startswith("linux")
    bwrap = _which("bwrap") if needs_bwrap else "n/a"
    env = _env_file()
    return [
        {"id": "anthropic", "name": "Anthropic API (Claude, pay per use)",
         "ready": bool(os.environ.get("ANTHROPIC_API_KEY")),
         "detail": "ANTHROPIC_API_KEY is set" if os.environ.get("ANTHROPIC_API_KEY") else "no ANTHROPIC_API_KEY",
         "fix": f"Paste a key from console.anthropic.com in the form below, or put ANTHROPIC_API_KEY=... in {env} and restart OpenClave."},
        {"id": "xai", "name": "xAI API (Grok, pay per use)",
         "ready": bool(os.environ.get("XAI_API_KEY")),
         "detail": "XAI_API_KEY is set" if os.environ.get("XAI_API_KEY") else "no XAI_API_KEY",
         "fix": f"Paste a key from console.x.ai in the form below, or put XAI_API_KEY=... in {env} and restart OpenClave."},
        {"id": "claude-cli", "name": "Claude CLI (your Claude Pro/Max subscription)",
         "ready": claude_in, "needs_sign_in": bool(claude) and not claude_in,
         "detail": (f"found {claude}" + ("" if claude_in else ", not signed in")) if claude
                   else f"'{CONFIG.claude_bin}' not found on PATH",
         "fix": ("Sign in to Claude below (Claude sign-in)." if claude
                 else "Install Claude Code (see README), run `claude` once and sign in, then restart OpenClave.")},
        {"id": "codex-cli", "name": "Codex CLI (your ChatGPT subscription)",
         "ready": bool(codex and bwrap),
         "detail": (f"found {codex}" if codex else f"'{CONFIG.codex_bin}' not found on PATH")
                   + ("" if bwrap else "; bubblewrap (bwrap) missing, Codex needs it for its sandbox"),
         "fix": "Install the Codex CLI and bubblewrap (see README), run `codex login`, then restart OpenClave."},
    ]


def ready_providers() -> set[str]:
    return {r["id"] for r in routes() if r["ready"]}


def loopback_bind(address: str | None = None) -> bool:
    """Whether [network] bind only listens on this computer."""
    import ipaddress
    address = (CONFIG.bind_address if address is None else address).strip().strip("[]").lower()
    if address == "localhost":
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def open_network_warning() -> str:
    """A warning when OpenClave listens beyond this computer without an
    access token: then everyone who can reach the port can use it. A warning
    only, since a private network (e.g. Tailscale) can make it a fair choice."""
    if loopback_bind() or CONFIG.access_token:
        return ""
    return (f"OpenClave listens on {CONFIG.bind_address}:{CONFIG.port} without an access token: anyone who "
            "can reach this port can use your agents and read your notes. Set [network] access_token in "
            "openclave.toml (or OPENCLAVE_ACCESS_TOKEN), or bind to 127.0.0.1.")


def ready_model_id() -> str:
    """The model a new agent gets: the default when its route is set up, else
    the first picker model that is (the default when none is)."""
    import llm_models
    ready = ready_providers()
    default = llm_models.get(llm_models.DEFAULT_MODEL_ID)
    if default.provider in ready:
        return default.id
    return next((m.id for m in llm_models.picker_models() if m.provider in ready), default.id)


# ── API key from the browser (Control Panel → Setup) ─────────────────────
# The same check as install.sh's check_key: GET /v1/models with the key.
KEY_VARS = {"anthropic": "ANTHROPIC_API_KEY", "xai": "XAI_API_KEY"}
KEY_NAMES = {"anthropic": "Anthropic", "xai": "xAI"}
_KEY_RE = re.compile(r"[A-Za-z0-9_\-.]{20,300}")
CHECK_TIMEOUT_S = 15


def check_key(provider: str, key: str) -> str:
    """"accepted" (200), "refused" (401/403) or "unknown" (anything else)."""
    if provider == "anthropic":
        url, headers = "https://api.anthropic.com/v1/models", {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        url, headers = "https://api.x.ai/v1/models", {"Authorization": f"Bearer {key}"}
    try:
        code = httpx.get(url, headers=headers, timeout=CHECK_TIMEOUT_S).status_code
    except httpx.HTTPError:
        return "unknown"
    return "accepted" if code == 200 else "refused" if code in (401, 403) else "unknown"


def _write_env_line(path: Path, name: str, value: str) -> None:
    """Replace or add NAME=value in the .env file, readable by its owner only."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    pattern = re.compile(rf"^\s*(export\s+)?{re.escape(name)}\s*=")
    lines = [line for line in lines if not pattern.match(line)] + [f"{name}={value}"]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _give_agents_a_ready_model() -> list[str]:
    """Agents whose model can't answer here move to ready_model_id(), the
    model install-time creation picks. Returns their names."""
    import agents
    import llm_models
    ready = ready_providers()
    target = ready_model_id()
    if llm_models.get(target).provider not in ready:
        return []
    moved = []
    for agent_id, cfg in agents.AGENTS.items():
        if llm_models.get(llm_models.selected_model_id(agent_id)).provider not in ready:
            llm_models.set_selected_model(agent_id, target)
            moved.append(cfg.name)
    return moved


def save_key(provider: str, key: str) -> dict:
    """Check the key with its provider, then save it to OPENCLAVE_HOME/.env and
    this process's environment, so it works without a restart (every route
    reads its key from os.environ when it calls). A refused key is not saved.
    The key itself is never returned or logged. Raises ValueError for input
    that can't be a key."""
    if provider not in KEY_VARS:
        raise ValueError("Pick Anthropic or xAI.")
    key = (key or "").strip()
    if not _KEY_RE.fullmatch(key):
        raise ValueError("That doesn't look like an API key. Copy the whole key, with nothing around it.")
    status = check_key(provider, key)
    name = KEY_NAMES[provider]
    if status == "refused":
        return {"status": status, "saved": False, "message": f"{name} refused that key. Check you copied all of it."}
    _write_env_line(CONFIG.home / ".env", KEY_VARS[provider], key)
    os.environ[KEY_VARS[provider]] = key
    import provider_credits
    provider_credits._cache.update(at=0.0, data=None)
    moved = _give_agents_a_ready_model()
    message = (f"{name} accepted the key. Saved." if status == "accepted"
               else f"Couldn't reach {name} to check the key, so it's saved unchecked. If it's wrong, the first chat will say so.")
    if moved:
        message += f" {', '.join(moved)} now {'uses' if len(moved) == 1 else 'use'} a model that can answer."
    return {"status": status, "saved": True, "message": message, "moved": moved}


def extras() -> list[dict]:
    """Optional parts: off is fine, they just add features."""
    qmd = _which(CONFIG.qmd_bin)
    voice_py = CONFIG.voice_python and Path(os.path.expanduser(CONFIG.voice_python)).is_file()
    voice_models = CONFIG.voice_model_dir and CONFIG.voice_model_dir.is_dir()
    return [
        {"id": "owner", "name": "Your name", "ready": bool(CONFIG.owner_name),
         "detail": CONFIG.owner_name or "not set; agents call you \"the user\"",
         "fix": "Set [owner] name in openclave.toml."},
        {"id": "search", "name": "Note search (qmd)", "ready": bool(qmd),
         "detail": f"found {qmd}" if qmd else "qmd not installed; agents can still read files they know",
         "fix": "Install qmd (see README) for full-text search over your notes."},
        {"id": "voice", "name": "Voice (local speech to text and speech)", "ready": bool(voice_py and voice_models),
         "detail": "configured" if voice_py and voice_models else "off",
         "fix": "See README, \"Voice\"."},
        {"id": "phone", "name": "Phone app over HTTPS", "ready": bool(CONFIG.public_url.startswith("https://")),
         "detail": CONFIG.public_url or "no [network] public_url; the phone app works on this computer only",
         "fix": "See README, \"Use it from your phone\"."},
        {"id": "network", "name": "Network access", "ready": not open_network_warning(),
         "detail": ("this computer only" if loopback_bind() else
                    "token required" if CONFIG.access_token else
                    f"open to the network on {CONFIG.bind_address} with no access token"),
         "fix": "Set [network] access_token in openclave.toml (or OPENCLAVE_ACCESS_TOKEN), then restart."},
    ]


def summary() -> dict:
    r = routes()
    return {"ready": any(x["ready"] for x in r), "routes": r, "extras": extras(),
            "home": str(CONFIG.home), "env_file": _env_file(),
            "notes_folder": str(CONFIG.vault_root), "agents_folder": str(CONFIG.agents_root)}


SUGGESTED_AGENTS = 4


def first_run() -> dict:
    """The home page's first steps: the manager, if any, and a few ready-made
    agents to add next (one per category, ones that need nothing extra set up,
    not added yet)."""
    import agents
    import catalog
    manager = agents.manager_id()
    suggested, categories = [], set()
    for t in catalog.listing():
        if t["manager"] or t["needs"] or t["installed"] or t["category"] in categories:
            continue
        categories.add(t["category"])
        suggested.append({"id": t["id"], "name": t["name"], "tagline": t["tagline"]})
    return {"agents": len(agents.AGENTS),
            "manager": {"id": manager, "name": agents.AGENTS[manager].name} if manager else None,
            "suggested": suggested[:SUGGESTED_AGENTS]}


def print_report() -> int:
    """`python3 run.py --check`: a plain-text report; exit 1 when no model can answer."""
    s = summary()
    print(f"OpenClave version:     {VERSION}")
    print(f"OpenClave data folder: {s['home']}")
    print(f"Notes folder:          {s['notes_folder']}")
    print(f"Agents folder:         {s['agents_folder']}")
    print(f"Keys file:             {s['env_file']}" + ("" if Path(s["env_file"]).is_file() else " (not created yet)"))
    print("\nModels (at least one needed):")
    for x in s["routes"]:
        print(f"  [{'ok' if x['ready'] else '--'}] {x['name']}: {x['detail']}")
        if not x["ready"]:
            print(f"       {x['fix']}")
    print("\nOptional:")
    for x in s["extras"]:
        print(f"  [{'ok' if x['ready'] else '--'}] {x['name']}: {x['detail']}")
        if x["id"] == "network" and not x["ready"]:
            print(f"       {x['fix']}")
    print(f"\nOpen http://{'localhost' if CONFIG.bind_address in ('127.0.0.1', '0.0.0.0') else CONFIG.bind_address}:{CONFIG.port}/")
    if not s["ready"]:
        print("\nNo model is set up yet: agents can't answer until one is. The quickest is an API key (see README).")
        return 1
    return 0
