"""Group chat channels the owner makes (2026-11-05).

Beside "# everyone" (every agent) and one room per clave (claves.py), the
owner can add channels with a name and the agents in them, e.g. "#garage"
with the maker and the HA agent. Each channel is a room like the others
(room.py): its own message file, @all means its members, and each member
answers in its own "Group chat · #name" conversation in a "Channels" folder.
@mentioning an agent who isn't in the channel adds them, so tagging is
enough to bring someone in. Deleting a channel removes it from the list and
keeps its messages file (renamed .deleted) and the agents' conversations.

State: OPENCLAVE_HOME/state/channels.json, a list of {id, name, members}.
Channel ids start with "ch-" so they never clash with clave ids.
"""
import re
import threading
import uuid

from agents import AGENTS
from config import CONFIG
from state_io import load_json, save_json

STATE_FILE = CONFIG.state_path("channels.json")
PREFIX = "ch-"
MAX_CHANNELS = 30
NAME_MAX = 40
FOLDER = "Channels"
_LOCK = threading.Lock()


def is_channel(room_id: str | None) -> bool:
    return bool(room_id) and room_id.startswith(PREFIX)


def _load() -> list[dict]:
    data = load_json(STATE_FILE, [])
    return [c for c in data if isinstance(c, dict) and is_channel(c.get("id"))] if isinstance(data, list) else []


def _save(items: list[dict]) -> None:
    save_json(STATE_FILE, items, ensure_ascii=False)


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", "-", (name or "").strip().lstrip("#").strip()).lower()
    if not name or len(name) > NAME_MAX or not re.fullmatch(r"[\w-]+", name):
        raise ValueError(f"a channel name is 1-{NAME_MAX} letters, digits or dashes")
    if name in ("everyone", "all"):
        raise ValueError("that name is taken by # everyone")
    return name


def _clean_members(members) -> list[str]:
    if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
        raise ValueError("members must be a list of agent ids")
    return [m for m in dict.fromkeys(members) if m in AGENTS]


def _shown(c: dict) -> dict:
    return {"id": c["id"], "name": c["name"], "members": [m for m in c["members"] if m in AGENTS]}


def list_channels() -> list[dict]:
    return [_shown(c) for c in _load()]


def get(channel_id: str) -> dict:
    found = next((c for c in _load() if c["id"] == channel_id), None)
    if not found:
        raise KeyError(channel_id)
    return _shown(found)


def members(channel_id: str) -> list[str]:
    return get(channel_id)["members"]


def create(name: str, members_: list[str] | None = None) -> dict:
    name = _clean_name(name)
    with _LOCK:
        items = _load()
        if len(items) >= MAX_CHANNELS:
            raise ValueError(f"at most {MAX_CHANNELS} channels")
        if any(c["name"] == name for c in items):
            raise ValueError(f"#{name} already exists")
        item = {"id": PREFIX + uuid.uuid4().hex[:8], "name": name, "members": _clean_members(members_ or [])}
        items.append(item)
        _save(items)
    return _shown(item)


def update(channel_id: str, name: str | None = None, members_: list[str] | None = None) -> dict:
    with _LOCK:
        items = _load()
        item = next((c for c in items if c["id"] == channel_id), None)
        if not item:
            raise KeyError(channel_id)
        if name is not None:
            clean = _clean_name(name)
            if any(c["name"] == clean and c["id"] != channel_id for c in items):
                raise ValueError(f"#{clean} already exists")
            item["name"] = clean
        if members_ is not None:
            item["members"] = _clean_members(members_)
        _save(items)
    return _shown(item)


def add_members(channel_id: str, agent_ids: list[str]) -> list[str]:
    """Agents @mentioned in a channel join it. Returns the ones newly added."""
    with _LOCK:
        items = _load()
        item = next((c for c in items if c["id"] == channel_id), None)
        if not item:
            raise KeyError(channel_id)
        new = [a for a in agent_ids if a in AGENTS and a not in item["members"]]
        if new:
            item["members"] += new
            _save(items)
    return new


def delete(channel_id: str, room_file) -> None:
    with _LOCK:
        items = _load()
        if not any(c["id"] == channel_id for c in items):
            raise KeyError(channel_id)
        _save([c for c in items if c["id"] != channel_id])
    if room_file.exists():
        room_file.replace(room_file.with_suffix(".deleted"))
