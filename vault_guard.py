"""The vault read deny rule: secret files, the state folder and confidential
folders that reads refuse. Part of vault.py's facade: import vault, not this module."""

import os
from pathlib import Path

import vault


class VaultReadRefused(PermissionError):
    """An agent asked for a file the deny rule keeps away from agents. The
    message is written for the model to relay to the user."""


# Files in OPENCLAVE_HOME that hold keys, the access token or the owner's
# private-details list. With the default vault_root (= OPENCLAVE_HOME) they sit
# inside the vault, so reads refuse them by name.
_HOME_SECRET_FILES = (".env", "openclave.toml", "private-strings.txt")
# Key and credential files that are never notes, wherever they are.
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx", ".gpg", ".age", ".jks", ".keystore", ".sqlite", ".db"}
_SECRET_NAME_PREFIXES = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")


def _secret_dirs() -> tuple[Path, ...]:
    """Folders with runtime state, tokens and the search index: the state
    folder plus the qmd and Garmin folders, which can be configured elsewhere."""
    return tuple(dict.fromkeys(p.resolve() for p in (
        vault.CONFIG.state_dir, vault.CONFIG.qmd_cache_home, vault.CONFIG.qmd_config_home, vault.CONFIG.garmin_token_dir)))


def _confidential_dirs() -> tuple[tuple[str, Path], ...]:
    return tuple((name, (vault.VAULT_ROOT / name).resolve()) for name in vault.CONFIG.confidential_folders)


def _within(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def confidential_refusal(name: str) -> str:
    return (f"refused: '{name}' is a confidential folder. The owner keeps it closed to agents, so nothing "
            "in it can be read, searched or written. Tell the user that if they ask for it.")


def _secret_refusal(path: Path) -> str:
    return (f"refused: {path.name} holds OpenClave settings, keys or other private files and is never "
            "readable by agents.")


def read_refusal(path: Path) -> str | None:
    """Why an agent may not read this file, or None when it may. The rule
    holds wherever vault_root points: OpenClave's own settings, keys and
    state; dotfiles and dot-folders (.env, .obsidian, .trash, .git); key and
    database files; and the configured confidential folders. Both the path as
    given and its symlink-resolved target are checked."""
    candidates = [Path(os.path.normpath(path.absolute())), path.resolve()]
    home = vault.CONFIG.home.resolve()
    for p in candidates:
        for name, folder in _confidential_dirs():
            if _within(p, folder):
                return confidential_refusal(name)
        if p.parent == home and p.name in _HOME_SECRET_FILES:
            return _secret_refusal(p)
        if any(_within(p, d) for d in _secret_dirs()):
            return _secret_refusal(p)
        if p.suffix.lower() in _SECRET_SUFFIXES or p.name.lower().startswith(_SECRET_NAME_PREFIXES):
            return _secret_refusal(p)
        # Dot-parts count below the agents folder when the file is in it, so an
        # agents folder that itself sits under a dot-folder stays readable.
        agents_root = vault.AGENTS_ROOT.resolve()
        bases = (agents_root,) if agents_root in p.parents else (vault.VAULT_ROOT, home)
        for base in bases:
            if base in p.parents and any(part.startswith(".") for part in p.relative_to(base).parts):
                return _secret_refusal(p)
    return None


def read_text_anywhere_in_vault(path: Path) -> str:
    """Whole-vault read access for agents, minus what read_refusal keeps out."""
    resolved = path.resolve()
    if resolved != vault.VAULT_ROOT and vault.VAULT_ROOT not in resolved.parents:
        raise PermissionError(f"path outside vault: {resolved}")
    refusal = read_refusal(path)
    if refusal:
        raise VaultReadRefused(refusal)
    return resolved.read_text(encoding="utf-8")
