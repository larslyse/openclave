"""Load user-specific OpenClave settings from OPENCLAVE_HOME/openclave.toml."""
from dataclasses import dataclass
import os
import re
import shutil
from pathlib import Path
import tomllib
from typing import Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _path(value, default: Path, home: Path) -> Path:
    if value is None or value == "":
        return default
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    return (path if path.is_absolute() else home / path).resolve()


def _program(value, default: str) -> str:
    """A command name, or its path in ~/.local/bin when it isn't on PATH: the
    Claude installer puts `claude` there, and a service or a fresh shell may
    not have that folder on PATH yet."""
    name = os.path.expanduser(str(value or default))
    if os.sep not in name and not shutil.which(name):
        local = Path.home() / ".local" / "bin" / name
        if local.is_file() and os.access(local, os.X_OK):
            return str(local)
    return name


@dataclass(frozen=True)
class Settings:
    home: Path
    vault_root: Path
    agents_root: Path
    project_backlog_file: Path | None
    shared_concept_file: Path
    tools_doc_path: Path
    skills_root: Path
    catalog_sources: tuple[Path, ...]
    pdf_tool: Path | None
    garmin_token_dir: Path
    confidential_folders: tuple[str, ...]
    qmd_bin: str
    qmd_index: str
    qmd_cache_home: Path
    qmd_config_home: Path
    claude_bin: str
    codex_bin: str
    voice_python: str
    voice_model_dir: Path | None
    bind_address: str
    port: int
    public_url: str
    extra_origins: tuple[str, ...]
    timezone: str
    owner_name: str
    claves_root: Path | None = None
    # Languages the owner's notes are written in, e.g. ("English", "Norwegian").
    # search_vault asks agents to search in each (qmd's keyword search does not
    # translate, so an English query misses a Norwegian note).
    note_languages: tuple[str, ...] = ()
    # [voice] second language beside English (2026-11-05): an ISO code for
    # speech-to-text ("no"), its name for the reply instruction, and the TTS
    # voice that speaks it. Empty = English only, no language switch.
    # [network] access_token: when set, every request needs it once (a login
    # page sets a cookie), except plain local ones on this computer.
    access_token: str = ""
    voice_second_language: str = ""
    voice_second_language_name: str = ""
    voice_second_voice: dict | None = None
    # [updates] check: look for a newer OpenClave every few hours (updater.py).
    update_check: bool = True
    # [updates] channel: "release" (the newest vX.Y.Z tag) or "main" (every
    # change on the main branch, for developers). See updater.py.
    update_channel: str = "release"
    # [cli] hide_private_files: run the Claude Code and Codex CLIs under
    # bubblewrap with private files masked (cli_sandbox.py). Off by default.
    hide_private_files: bool = False

    @property
    def qmd_env(self) -> dict[str, str]:
        return {
            "XDG_CACHE_HOME": str(self.qmd_cache_home),
            "XDG_CONFIG_HOME": str(self.qmd_config_home),
        }

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    def state_path(self, filename: str) -> Path:
        """Return a runtime-state path beneath OPENCLAVE_HOME/state."""
        if Path(filename).name != filename:
            raise ValueError("state filename must be a single path component")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self.state_dir / filename


def _second_language(voice: Mapping) -> dict:
    code = str(voice.get("second_language", "") or "").strip().lower()
    if not code:
        return {}
    if not re.fullmatch(r"[a-z]{2,3}", code) or code == "en":
        raise ValueError("voice.second_language must be an ISO language code like \"no\" or \"de\" (not en)")
    provider, voice_id = str(voice.get("second_voice_provider", "") or ""), str(voice.get("second_voice_id", "") or "")
    return {"voice_second_language": code,
            "voice_second_language_name": str(voice.get("second_language_name", "") or code),
            "voice_second_voice": {"provider": provider, "voice_id": voice_id} if provider and voice_id else None}


def _languages(value) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("owner.note_languages must be a list of language names")
    return tuple(v.strip() for v in value if v.strip())


def load_config(path: Path | str | None = None, *, environ: Mapping[str, str] | None = None) -> Settings:
    """Read TOML settings, filling omitted values with safe local defaults.

    Relative filesystem paths are resolved beneath OPENCLAVE_HOME. An absent
    config file is normal on first run; malformed TOML or invalid section
    shapes raise a clear error instead of silently ignoring user settings.
    """
    env = os.environ if environ is None else environ
    home_value = env.get("OPENCLAVE_HOME") or "~/.openclave"
    home = Path(os.path.expandvars(os.path.expanduser(home_value))).resolve()
    config_path = Path(path).expanduser() if path is not None else home / "openclave.toml"
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid OpenClave config at {config_path}: {exc}") from exc

    def section(name: str) -> dict:
        value = raw.get(name, {})
        if not isinstance(value, dict):
            raise ValueError(f"config section [{name}] must be a table")
        return value

    paths = section("paths")
    search = section("search")
    cli = section("cli")
    voice = section("voice")
    network = section("network")
    time = section("time")
    owner = section("owner")
    update_check = section("updates").get("check", True)
    if not isinstance(update_check, bool):
        raise ValueError("updates.check must be true or false")
    update_channel = section("updates").get("channel", "release")
    if update_channel not in ("release", "main"):
        raise ValueError('updates.channel must be "release" or "main"')
    hide_private_files = cli.get("hide_private_files", False)
    if not isinstance(hide_private_files, bool):
        raise ValueError("cli.hide_private_files must be true or false")
    catalog_sources = section("catalog").get("sources", [])
    if not isinstance(catalog_sources, list) or not all(isinstance(s, str) and s for s in catalog_sources):
        raise ValueError("catalog.sources must be a list of folder paths")

    vault_root = _path(paths.get("vault_root"), home, home)
    agents_root = _path(paths.get("agents_root"), vault_root / "agents", home)
    project_backlog_value = paths.get("project_backlog", "")
    project_backlog_file = None
    if project_backlog_value:
        project_backlog_file = Path(os.path.expandvars(os.path.expanduser(str(project_backlog_value))))
        if not project_backlog_file.is_absolute():
            project_backlog_file = vault_root / project_backlog_file
        project_backlog_file = project_backlog_file.resolve()
        if project_backlog_file.suffix.lower() != ".md" or vault_root not in project_backlog_file.parents:
            raise ValueError("paths.project_backlog must be a Markdown file inside paths.vault_root")
    pdf_tool_value = paths.get("pdf_tool", "")
    voice_model_value = voice.get("model_dir", "")
    confidential = section("privacy").get("confidential_folders", [])
    if not isinstance(confidential, list) or not all(isinstance(item, str) and item for item in confidential):
        raise ValueError("privacy.confidential_folders must be a list of non-empty strings")

    port = network.get("port", 8777)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("network.port must be an integer from 1 to 65535")
    timezone = str(time.get("timezone", "UTC")).strip() or "UTC"
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone in [time].timezone: {timezone}") from exc
    public_url = str(network.get("public_url", "")).strip().rstrip("/")
    if public_url:
        parsed_url = urlsplit(public_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("network.public_url must be an http(s) URL with a host")

    extra_origins = network.get("extra_origins", [])
    if not isinstance(extra_origins, list) or not all(isinstance(o, str) and urlsplit(o).netloc for o in extra_origins):
        raise ValueError("network.extra_origins must be a list of origins like \"http://host:port\"")

    return Settings(
        home=home,
        vault_root=vault_root,
        agents_root=agents_root,
        project_backlog_file=project_backlog_file,
        shared_concept_file=_path(paths.get("shared_concept_file"), home / "openclave.md", home),
        tools_doc_path=_path(paths.get("tools_doc"), home / "docs" / "tools.md", home),
        skills_root=_path(paths.get("skills_root"), home / "skills", home),
        catalog_sources=tuple(_path(s, home, home) for s in catalog_sources),
        pdf_tool=_path(pdf_tool_value, home / "bin" / "vault-pdf.py", home) if pdf_tool_value else None,
        garmin_token_dir=_path(paths.get("garmin_token_dir"), home / "state" / "garmin", home),
        confidential_folders=tuple(confidential),
        qmd_bin=_program(search.get("qmd_bin"), "qmd"),
        qmd_index=str(search.get("qmd_index", "openclave")),
        qmd_cache_home=_path(search.get("xdg_cache_home"), home / "state" / "qmd-cache", home),
        qmd_config_home=_path(search.get("xdg_config_home"), home / "state" / "qmd-config", home),
        claude_bin=_program(cli.get("claude"), "claude"),
        codex_bin=_program(cli.get("codex"), "codex"),
        voice_python=str(voice.get("python", "")),
        voice_model_dir=_path(voice_model_value, home / "models" / "voice", home) if voice_model_value else None,
        bind_address=str(network.get("bind") or "127.0.0.1"),
        port=port,
        public_url=public_url,
        extra_origins=tuple(o.rstrip("/") for o in extra_origins),
        timezone=timezone,
        owner_name=str(owner.get("name", "")).strip(),
        claves_root=_path(paths.get("claves_root"), agents_root.parent / "Claves", home),
        note_languages=_languages(owner.get("note_languages", [])),
        access_token=str(env.get("OPENCLAVE_ACCESS_TOKEN") or network.get("access_token", "") or "").strip(),
        update_check=update_check,
        update_channel=update_channel,
        hide_private_files=hide_private_files,
        **_second_language(voice),
    )


def load_env_file(path: Path) -> None:
    """Read KEY=value lines (API keys, tokens) from OPENCLAVE_HOME/.env into the
    environment, so a plain `python3 run.py` finds them. Variables that are
    already set (a systemd EnvironmentFile, an export in the shell) win."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and value:
            os.environ.setdefault(key, value)


load_env_file(Path(os.path.expandvars(os.path.expanduser(os.environ.get("OPENCLAVE_HOME") or "~/.openclave"))) / ".env")
CONFIG = load_config()
