"""Scoped write access to the main vault, per agent (2026-11-05).

Agents write only in their own folder. The owner can give one agent a few
vault folders beyond that (Control Panel → Edit → "Vault folders it may
write in"), e.g. a blog folder for a writing agent or a whole area for a
librarian. Stored in the agent's agent.json as "vault_write" (vault-relative
folder paths). The optional tool write_vault_note then appends to or creates
Markdown notes under those folders only, through the same checked writer the
manager's approved proposals use (vault.write_main_vault_note): never in
agent folders, confidential or hidden folders, never deletes, never makes
folders.
"""
import json
from pathlib import Path

import agents
import vault

MAX_FOLDERS = 10


def _agent_file(agent_id: str) -> Path:
    return vault.agent_root(agent_id) / agents.AGENT_FILE


def folders(agent_id: str) -> list[str]:
    try:
        data = json.loads(_agent_file(agent_id).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return []
    value = data.get("vault_write") or []
    return [str(f) for f in value if isinstance(f, str)] if isinstance(value, list) else []


def _check_folder(rel: str) -> str:
    rel = rel.strip().strip("/")
    if not rel:
        raise ValueError("empty folder")
    # Reuse the main-vault checks on a note path inside it.
    try:
        target = vault.main_vault_target(f"{rel}/x.md").parent
    except vault.MainVaultError as exc:
        raise ValueError(f"{rel}: {exc}") from exc
    if not target.is_dir():
        raise ValueError(f"{rel}: no such folder in the vault")
    return str(target.relative_to(vault.VAULT_ROOT.resolve()))


def set_folders(agent_id: str, wanted: list[str]) -> list[str]:
    clean = list(dict.fromkeys(_check_folder(f) for f in wanted if f.strip()))
    if len(clean) > MAX_FOLDERS:
        raise ValueError(f"at most {MAX_FOLDERS} folders")
    f = _agent_file(agent_id)
    data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"id": agent_id}
    if clean:
        data["vault_write"] = clean
    else:
        data.pop("vault_write", None)
    f.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return clean


def write(agent_id: str, path: str, content: str, mode: str, summary: str) -> str:
    allowed = folders(agent_id)
    if not allowed:
        return ("refused: you have no vault folders to write in. The owner can add some in "
                "Control Panel → Agents → Edit.")
    try:
        target = vault.main_vault_target(path)
    except vault.MainVaultError as exc:
        return f"refused: {exc}"
    root = vault.VAULT_ROOT.resolve()
    if not any(target.is_relative_to((root / a).resolve()) for a in allowed):
        return f"refused: {path} is outside your vault folders ({', '.join(allowed)})"
    try:
        written = vault.write_main_vault_note(str(target.relative_to(root)), content, mode, summary)
    except vault.MainVaultError as exc:
        return f"refused: {exc}"
    return f"Success! {'Appended to' if mode == 'append' else 'Created'} {written}"


def prompt_line(agent_id: str) -> str:
    allowed = folders(agent_id)
    if not allowed:
        return ""
    return ("Besides your own folder you may write notes in these vault folders with write_vault_note "
            f"(append to or create .md notes, never delete): {', '.join(allowed)}. Search first and "
            "append to the note that covers it; follow nearby notes' conventions.\n")
