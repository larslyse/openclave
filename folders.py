"""Chat folders: plain buckets for an agent's chats.

A chat is in a folder when its front matter says ``Folder: <name>``. The
agent's ``folders.json`` keeps the folder names in order, so an empty folder
stays put. That's all: no pages, no status, nothing sent to the model.
"""
import json

import vault

FOLDERS_FILE = "folders.json"
MAX_NAME = 60


def _file(agent_id: str):
    return vault.agent_root(agent_id) / FOLDERS_FILE


def _clean(name: str) -> str:
    name = " ".join((name or "").split())[:MAX_NAME]
    if not name:
        raise ValueError("a folder needs a name")
    return name


def _saved(agent_id: str) -> list[str]:
    try:
        data = json.loads(_file(agent_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    names = data.get("folders") if isinstance(data, dict) else None
    return [str(n) for n in names if isinstance(n, str) and n.strip()] if isinstance(names, list) else []


def _save(agent_id: str, names: list[str]) -> None:
    vault.write_text(agent_id, _file(agent_id), json.dumps({"folders": names}, indent=2, ensure_ascii=False) + "\n")


def list_folders(agent_id: str) -> list[dict]:
    """Saved folders in order, plus any a chat names that isn't saved yet."""
    counts: dict[str, int] = {}
    for c in vault.list_conversations(agent_id):
        if c.get("folder"):
            counts[c["folder"]] = counts.get(c["folder"], 0) + 1
    names = _saved(agent_id)
    names += sorted(n for n in counts if n not in names)
    return [{"name": n, "chats": counts.get(n, 0)} for n in names]


def create_folder(agent_id: str, name: str) -> dict:
    name = _clean(name)
    names = _saved(agent_id)
    if name.lower() in (n.lower() for n in names):
        raise FileExistsError(f"there is already a folder called {name}")
    _save(agent_id, [*names, name])
    return {"name": name, "chats": 0}


def _each_chat(agent_id: str):
    d = vault.conversations_dir(agent_id)
    for sub in (d, d / vault.ARCHIVED_DIR):
        yield from (sub.glob("*.md") if sub.is_dir() else ())


def _set_field(agent_id: str, f, name: str | None) -> bool:
    with vault.file_lock(agent_id, f) as scoped:
        if not scoped.exists():
            return False
        fm, body = vault.split_frontmatter(scoped.read_text(encoding="utf-8"))
        if name:
            fm["Folder"] = name
        else:
            fm.pop("Folder", None)
        vault._write_keeping_mtime(scoped, vault.render_frontmatter(fm, body))  # no jump to the top
    return True


def rename_folder(agent_id: str, old: str, new: str) -> dict:
    new = _clean(new)
    names = _saved(agent_id)
    if new != old and new.lower() in (n.lower() for n in names):
        raise FileExistsError(f"there is already a folder called {new}")
    for f in _each_chat(agent_id):
        fm, _ = vault.split_frontmatter(f.read_text(encoding="utf-8"))
        if fm.get("Folder") == old:
            _set_field(agent_id, f, new)
    _save(agent_id, [new if n == old else n for n in names] if old in names else [*names, new])
    return {"name": new}


def delete_folder(agent_id: str, name: str) -> None:
    """The folder goes; its chats stay, just unfiled."""
    for f in _each_chat(agent_id):
        fm, _ = vault.split_frontmatter(f.read_text(encoding="utf-8"))
        if fm.get("Folder") == name:
            _set_field(agent_id, f, None)
    _save(agent_id, [n for n in _saved(agent_id) if n != name])


def new_conversation(agent_id: str, name: str) -> str:
    """An empty chat in a folder (the folder's +). Named from its first
    message like any new chat (vault.append_turn fills an empty Title)."""
    name = _clean(name)
    if name not in _saved(agent_id):
        _save(agent_id, [*_saved(agent_id), name])
    return vault.create_conversation(agent_id, "", {"Folder": name})


def set_conversation_folder(agent_id: str, conversation_id: str, name: str | None) -> bool:
    """Put a chat (live or archived) in a folder, or take it out (None)."""
    if name:
        name = _clean(name)
        if name not in _saved(agent_id):
            _save(agent_id, [*_saved(agent_id), name])
    live = vault.conversation_file(agent_id, conversation_id)
    f = live if live.exists() else vault.archived_conversation_file(agent_id, conversation_id)
    return _set_field(agent_id, f, name)
