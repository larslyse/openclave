"""Hiding private files from the Claude Code and Codex CLI subprocesses
(`[cli] hide_private_files`, off by default).

OpenClave's own tools refuse private files (vault_guard.read_refusal), but the
CLIs have their own file access: Codex's shell can read any file the user can,
and an agent's extra MCP servers run with the user's full rights. With the
setting on, the providers pass their command through `wrap`, which runs it
under bubblewrap (bwrap):

- The whole filesystem is bound read-only, then the places the run writes to
  are bound read-write: OPENCLAVE_HOME, the vault, the agents folder (with the
  agent's own folder in it), the CLI's own settings folder (~/.claude or
  ~/.codex) and the per-run temp files the provider names.
- /tmp (and the system temp dir) is a fresh empty tmpfs, so other programs'
  temp files are out of sight; /dev is a minimal one; the run gets its own PID
  namespace and /proc, so the service's own process (and its environment) is
  not visible.
- Private paths are masked last: an empty read-only tmpfs over folders,
  /dev/null over single files. `private_paths` is the one list.
- Network access stays (the CLI must reach its provider). There is no
  `--new-session`, so the whole tree stays in the process group that
  llm_runs.cancel_running kills; `--die-with-parent` takes it down if the
  bwrap process dies.

The MCP vault server is a child of the CLI, so it runs inside the same mounts
and must still work: it imports config and vault (openclave.toml, the vault,
the state folder, the search index). One mount namespace cannot show a file
to the server and hide it from the CLI's own tools, so the design hides only
what neither needs:

- `.env` and `private-strings.txt` in OPENCLAVE_HOME. The keys from `.env` are
  already in the service's environment, which the CLI inherits; the Codex
  provider forwards their names to the vault server explicitly, because Codex
  gives MCP servers a reduced environment.
- `openclave.toml` is replaced by a copy without `[network] access_token`
  (kept in the state folder), so the server reads the same paths and
  confidential folders but not the login token.
- The state folder stays visible (the server reads and writes it, search
  needs its index), except the web-push private key and the Garmin tokens.
- The confidential folders, the other CLI's credentials, ssh/gpg agent
  sockets and the well-known credential locations in HOME_SECRETS.

What stays readable: environment variables, the rest of the state folder,
and any file outside the mask list that the user can read. The vault stays
writable for the whole run because the vault server writes there; Codex's
own `-s workspace-write` sandbox still limits its shell to the agent folder.
"""
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

import vault
import vault_guard


class SandboxUnavailable(RuntimeError):
    """The setting is on but the wrapper can't be built. The message is the
    one-line reply the owner sees; the command is never run unwrapped."""


NO_BWRAP = "hide_private_files is on but bubblewrap isn't available: install it or turn the setting off"

# Well-known credential locations under the home folder, masked when they
# exist: keys, cloud and package-registry logins, password stores, keyrings
# and browser profiles (saved passwords, cookies).
HOME_SECRETS = (
    ".ssh", ".gnupg", ".aws", ".azure", ".config/gcloud", ".kube", ".docker",
    ".password-store", ".local/share/keyrings", ".pki", ".mozilla", ".thunderbird",
    ".config/google-chrome", ".config/chromium", ".config/BraveSoftware",
    ".config/microsoft-edge", ".config/gh", ".config/rclone",
    ".netrc", ".git-credentials", ".npmrc", ".pypirc", ".vault-token",
)

# Each CLI's own settings folder and login (bound read-write for its own runs;
# the other CLI's login is masked).
_CLI_HOME = {"claude": (".claude", ".claude.json", ".claude.json.backup"), "codex": (".codex",)}
_CLI_LOGIN = {"claude": ".claude/.credentials.json", "codex": ".codex/auth.json"}

_bwrap_works = False


def enabled() -> bool:
    return bool(getattr(vault.CONFIG, "hide_private_files", False))


def _existing(paths) -> list[Path]:
    """Resolved (symlinks followed), existing, de-duplicated paths."""
    out = []
    for p in paths:
        if p is None:
            continue
        try:
            resolved = Path(p).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.exists() and resolved not in out:
            out.append(resolved)
    return out


def private_paths(cli: str, env: dict | None = None) -> list[Path]:
    """Every path a sandboxed run of this CLI must not see, resolved and
    existing only."""
    env = os.environ if env is None else env
    config = vault.CONFIG
    home = Path.home()
    paths = [config.home / name for name in vault_guard._HOME_SECRET_FILES if name != "openclave.toml"]
    paths += [config.state_dir / "push_keys.json", config.garmin_token_dir, env.get("OPENCLAVE_PRIVATE_STRINGS")]
    paths += [folder for _name, folder in vault_guard._confidential_dirs()]
    paths += [home / rel for rel in HOME_SECRETS]
    paths += [home / login for other, login in _CLI_LOGIN.items() if other != cli]
    sock = env.get("SSH_AUTH_SOCK")
    paths += [sock, f"/run/user/{os.getuid()}/gnupg", f"/run/user/{os.getuid()}/keyring"]
    return _existing(paths)


def _within(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def writable_paths(cli: str, agent_id: str, extra=(), masks=()) -> list[Path]:
    """Where a run writes: OpenClave's data and vault (the vault server
    writes there), the agent folder, the CLI's own settings and the per-run
    files. Nested paths are dropped (the outer bind covers them). One inside
    a masked path can't be both, so that fails closed."""
    config = vault.CONFIG
    home = Path.home()
    paths = _existing([config.home, vault.VAULT_ROOT, vault.AGENTS_ROOT, config.claves_root,
                       vault.agent_root(agent_id), *(home / rel for rel in _CLI_HOME.get(cli, ())), *extra])
    for path in paths:
        for mask in masks:
            if _within(path, mask):
                raise SandboxUnavailable(f"hide_private_files is on but {path} is inside a hidden path ({mask}): "
                                         "move it or turn the setting off")
    return [p for p in paths if not any(o != p and _within(p, o) for o in paths)]


def sanitized_toml() -> Path | None:
    """openclave.toml without `[network] access_token`, kept in the state
    folder and bound over the real file. None when there is no toml."""
    source = vault.CONFIG.home / "openclave.toml"
    if not source.is_file():
        return None
    text = source.read_text(encoding="utf-8")
    clean = re.sub(r"(?m)^[ \t]*access_token[ \t]*=.*$", 'access_token = ""', text)
    try:
        if tomllib.loads(clean).get("network", {}).get("access_token", ""):
            raise ValueError
    except (tomllib.TOMLDecodeError, ValueError, AttributeError):
        raise SandboxUnavailable("hide_private_files is on but the access token in openclave.toml "
                                 "can't be hidden: write it as access_token = \"...\" on one line") from None
    target = vault.CONFIG.state_path("cli-sandbox-openclave.toml")
    if not target.is_file() or target.read_text(encoding="utf-8") != clean:
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".cli-sandbox-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(clean)
        os.replace(tmp, target)
    return target


def _check_bwrap() -> str:
    global _bwrap_works
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise SandboxUnavailable(NO_BWRAP)
    if not _bwrap_works:
        probe = subprocess.run([bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                                "--unshare-pid", "--die-with-parent", "true"],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
        if probe.returncode != 0:
            detail = probe.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output"]
            raise SandboxUnavailable(f"hide_private_files is on but bubblewrap can't start here ({detail[0][:200]}): "
                                     "fix it or turn the setting off")
        _bwrap_works = True
    return bwrap


def wrap(argv: list[str], *, cli: str, agent_id: str, env: dict, cwd: str | None = None,
         writable=(), readable=()) -> list[str]:
    """The command to run instead of `argv`: the same command under bwrap
    with private paths masked. `writable`/`readable` are the per-run files
    the provider created in the temp dir (hidden by the fresh /tmp
    otherwise). Raises SandboxUnavailable instead of returning anything
    that would run unwrapped."""
    bwrap = _check_bwrap()
    masks = private_paths(cli, env)
    rw = writable_paths(cli, agent_id, writable, masks)
    args = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--unshare-pid", "--die-with-parent"]
    for tmp in dict.fromkeys(["/tmp", str(Path(tempfile.gettempdir()).resolve())]):
        args += ["--tmpfs", tmp]
    for path in sorted(rw, key=lambda p: len(p.parts)):
        args += ["--bind", str(path), str(path)]
    for path in _existing(readable):
        args += ["--ro-bind", str(path), str(path)]
    toml = sanitized_toml()
    if toml:
        args += ["--ro-bind", str(toml), str((vault.CONFIG.home / "openclave.toml").resolve())]
    for path in masks:
        if path.is_dir():
            args += ["--tmpfs", str(path), "--remount-ro", str(path)]
        else:
            args += ["--ro-bind", "/dev/null", str(path)]
    if cwd:
        args += ["--chdir", str(cwd)]
    return [*args, "--", *argv]


def env_file_names(env: dict) -> list[str]:
    """Names of the variables OPENCLAVE_HOME/.env sets that are in `env`,
    plus OPENCLAVE_HOME: what the vault server gets by name under Codex,
    since it can't read the masked .env itself."""
    names = ["OPENCLAVE_HOME"] if "OPENCLAVE_HOME" in env else []
    try:
        lines = (vault.CONFIG.home / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        key = line.strip().removeprefix("export ").split("=", 1)[0].strip()
        if "=" in line and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and key in env and key not in names:
            names.append(key)
    return names
