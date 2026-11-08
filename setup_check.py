"""What is set up on this computer: which model routes can answer, and the
optional extras. One source for the home page's first-run card, the Control
Panel's Setup section, the model pickers' "not set up" rows and
`python3 run.py --check`.

Only local facts (an environment variable is set, a program is on PATH), so
it is instant and never calls a provider. A key that is set but wrong shows
up as an error in the first chat instead.
"""
import os
import shutil
import sys
from pathlib import Path

from config import CONFIG

# The model routes, in the order a new user is pointed to them.
PROVIDERS = ("anthropic", "xai", "claude-cli", "codex-cli")


def _which(program: str) -> str | None:
    return shutil.which(os.path.expanduser(program))


def _env_file() -> str:
    return str(CONFIG.home / ".env")


def routes() -> list[dict]:
    """One row per model route: id (= llm_models provider), name, ready, detail, fix."""
    claude, codex = _which(CONFIG.claude_bin), _which(CONFIG.codex_bin)
    needs_bwrap = sys.platform.startswith("linux")
    bwrap = _which("bwrap") if needs_bwrap else "n/a"
    env = _env_file()
    return [
        {"id": "anthropic", "name": "Anthropic API (Claude, pay per use)",
         "ready": bool(os.environ.get("ANTHROPIC_API_KEY")),
         "detail": "ANTHROPIC_API_KEY is set" if os.environ.get("ANTHROPIC_API_KEY") else "no ANTHROPIC_API_KEY",
         "fix": f"Create a key at console.anthropic.com, put ANTHROPIC_API_KEY=... in {env}, restart OpenClave."},
        {"id": "xai", "name": "xAI API (Grok, pay per use)",
         "ready": bool(os.environ.get("XAI_API_KEY")),
         "detail": "XAI_API_KEY is set" if os.environ.get("XAI_API_KEY") else "no XAI_API_KEY",
         "fix": f"Create a key at console.x.ai, put XAI_API_KEY=... in {env}, restart OpenClave."},
        {"id": "claude-cli", "name": "Claude CLI (your Claude Pro/Max subscription)",
         "ready": bool(claude),
         "detail": f"found {claude}" if claude else f"'{CONFIG.claude_bin}' not found on PATH",
         "fix": "Install Claude Code (see README), run `claude` once and sign in, then restart OpenClave."},
        {"id": "codex-cli", "name": "Codex CLI (your ChatGPT subscription)",
         "ready": bool(codex and bwrap),
         "detail": (f"found {codex}" if codex else f"'{CONFIG.codex_bin}' not found on PATH")
                   + ("" if bwrap else "; bubblewrap (bwrap) missing, Codex needs it for its sandbox"),
         "fix": "Install the Codex CLI and bubblewrap (see README), run `codex login`, then restart OpenClave."},
    ]


def ready_providers() -> set[str]:
    return {r["id"] for r in routes() if r["ready"]}


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
    ]


def summary() -> dict:
    r = routes()
    return {"ready": any(x["ready"] for x in r), "routes": r, "extras": extras(),
            "home": str(CONFIG.home), "env_file": _env_file(),
            "notes_folder": str(CONFIG.vault_root), "agents_folder": str(CONFIG.agents_root)}


def print_report() -> int:
    """`python3 run.py --check`: a plain-text report; exit 1 when no model can answer."""
    s = summary()
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
    print(f"\nOpen http://{'localhost' if CONFIG.bind_address in ('127.0.0.1', '0.0.0.0') else CONFIG.bind_address}:{CONFIG.port}/")
    if not s["ready"]:
        print("\nNo model is set up yet: agents can't answer until one is. The quickest is an API key (see README).")
        return 1
    return 0
