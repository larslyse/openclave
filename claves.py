"""Claves (2026-11-04): named groups of agents, each with its own group chat.

A clave is a membership list, not a folder of agents: agents stay in
``agents_root``. An agent is in one clave; to have "the same" agent in another
clave you clone it (agent_factory.clone_agent: same name, persona, mission,
tools and skills, its own empty memory and chats), so claves stay separate.
The manager is the exception: it looks after the whole install, so it is in
every clave's group chat and tabs, and may lead any clave. Each clave is a small
vault folder ``<claves_root>/<Name>/`` holding

- ``clave.md``: front matter Id, Name, Lead, Members (ordered agent ids), Created
- ``backlog.md``: the clave's own backlog

The Id never changes, so renaming a clave only changes its Name (the folder
keeps the name it was created with). With the "Multiple claves" setting off
(the default) none of this shows and the group chat is the one "everyone" room.
"""
import re
import shutil
import threading
from dataclasses import dataclass, field
from datetime import date

import service_settings
from agents import AGENTS
from config import CONFIG

CLAVE_FILE = "clave.md"
EVERYONE = "everyone"  # the built-in room with every agent; not a clave
MAX_NAME = 40
ARCHIVE = "_Archived"
_LOCK = threading.RLock()


def root():
    return CONFIG.claves_root or CONFIG.agents_root.parent / "Claves"


@dataclass
class Clave:
    id: str
    name: str
    folder: str
    lead: str | None = None
    members: list[str] = field(default_factory=list)
    created: str = ""

    def as_dict(self) -> dict:
        members = [a for a in self.members if a in AGENTS]
        lead = self.lead if self.lead in members else None
        return {"id": self.id, "name": self.name, "folder": self.folder, "lead": lead, "members": members}


def enabled() -> bool:
    return bool(service_settings.load().get("multiple_claves"))


def _split(text: str) -> tuple[dict, str]:
    import vault
    return vault.split_frontmatter(text)


def _render(fm: dict, body: str) -> str:
    import vault
    return vault.render_frontmatter(fm, body)


def _read(folder) -> Clave | None:
    try:
        fm, _ = _split((folder / CLAVE_FILE).read_text(encoding="utf-8"))
    except OSError:
        return None
    clave_id = str(fm.get("Id") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", clave_id) or clave_id == EVERYONE:
        return None
    members = fm.get("Members") or []
    members = [str(m) for m in members if isinstance(m, str) and m] if isinstance(members, list) else []
    lead = fm.get("Lead")
    return Clave(id=clave_id, name=str(fm.get("Name") or folder.name), folder=folder.name,
                 lead=str(lead) if lead else None, members=list(dict.fromkeys(members)),
                 created=str(fm.get("Created") or ""))


def list_claves() -> list[Clave]:
    """Every clave, oldest first (then by name)."""
    base = root()
    if not base.is_dir():
        return []
    found, seen = [], set()
    for folder in sorted(base.iterdir()):
        if not folder.is_dir() or folder.name.startswith(("_", ".")):
            continue
        clave = _read(folder)
        if clave and clave.id not in seen:
            seen.add(clave.id)
            found.append(clave)
    return sorted(found, key=lambda c: (c.created, c.name.lower()))


def get(clave_id: str) -> Clave:
    for clave in list_claves():
        if clave.id == clave_id:
            return clave
    raise KeyError(f"no clave '{clave_id}'")


def _manager() -> str | None:
    import agents as agents_mod
    return agents_mod.manager_id()


def members(clave_id: str) -> list[str]:
    """Agent ids in a room: every agent for "everyone", else the clave's
    members plus the manager (who is in every clave)."""
    if clave_id == EVERYONE:
        return list(AGENTS)
    found = get(clave_id).as_dict()["members"]
    manager = _manager()
    return found + [manager] if manager and manager not in found else found


def clave_of(agent_id: str) -> str | None:
    """The one clave an agent is in (None for the manager or an agent in none)."""
    if agent_id == _manager():
        return None
    return next((c.id for c in list_claves() if agent_id in c.members), None)


def _leave_others(agent_id: str, keep: str | None) -> None:
    """One agent, one clave: take it out of every clave but `keep`. The
    manager may be in any number of them."""
    if agent_id == _manager():
        return
    for clave in list_claves():
        if clave.id != keep and agent_id in clave.members:
            clave.members = [a for a in clave.members if a != agent_id]
            if clave.lead == agent_id:
                clave.lead = None
            _write(clave)


def name_taken_in_clave(name: str, clave_id: str | None, *, exclude: str | None = None) -> bool:
    """Clones share their name, but within one clave names must differ
    (@mentions go by name)."""
    ids = members(clave_id) if clave_id else list(AGENTS)
    return any(a != exclude and AGENTS[a].name.lower() == name.lower() for a in ids if a in AGENTS)


def name_of(clave_id: str) -> str:
    return "everyone" if clave_id == EVERYONE else get(clave_id).name


def claves_for(agent_id: str) -> list[str]:
    return [c.id for c in list_claves() if agent_id in c.members]


def _write(clave: Clave) -> None:
    path = root() / clave.folder / CLAVE_FILE
    try:
        _, body = _split(path.read_text(encoding="utf-8"))
    except OSError:
        body = ""
    fm = {"Id": clave.id, "Name": clave.name, "Lead": clave.lead or "", "Members": clave.members,
          "Created": clave.created or str(date.today())}
    body = body or (f"# {clave.name}\n\nWhat this clave is for, and how its agents work together.\n")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(_render(fm, body), encoding="utf-8")
    tmp.replace(path)


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name or len(name) > MAX_NAME or not re.fullmatch(r"[\w][\w .&'-]*", name):
        raise ValueError(f"Clave name: 1-{MAX_NAME} letters, digits, spaces, dots, dashes.")
    return name


def _check_members(ids: list[str]) -> list[str]:
    unknown = [a for a in ids if a not in AGENTS]
    if unknown:
        raise ValueError(f"unknown agent(s): {', '.join(unknown)}")
    return list(dict.fromkeys(ids))


def create(name: str, members: list[str] | None = None, lead: str | None = None) -> dict:
    name = _clean_name(name)
    members = _check_members(members or [])
    with _LOCK:
        for agent_id in members:
            _leave_others(agent_id, None)
        taken = {c.id for c in list_claves()} | {EVERYONE}
        if any(c.name.lower() == name.lower() for c in list_claves()):
            raise ValueError(f"There is already a clave called '{name}'.")
        base_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "clave"
        clave_id, n = base_id, 2
        while clave_id in taken:
            clave_id, n = f"{base_id}-{n}", n + 1
        folder = root() / name
        if folder.exists():
            raise ValueError(f"A folder named '{name}' already exists in Claves.")
        folder.mkdir(parents=True)
        (folder / "backlog.md").write_text(f"---\nUpdated: {date.today()}\n---\n\n# {name} — Backlog\n\n", encoding="utf-8")
        clave = Clave(id=clave_id, name=name, folder=name, members=members,
                      lead=lead if lead in members else None, created=str(date.today()))
        _write(clave)
    return clave.as_dict()


_UNSET = object()


def update(clave_id: str, *, name: str | None = None, members: list[str] | None = None,
           lead=_UNSET) -> dict:
    """Rename, set members (ordered) and/or the lead ("" or None = no lead)."""
    with _LOCK:
        if members is not None:
            for agent_id in _check_members(members):
                _leave_others(agent_id, clave_id)
        clave = get(clave_id)
        if name is not None:
            name = _clean_name(name)
            if any(c.id != clave_id and c.name.lower() == name.lower() for c in list_claves()):
                raise ValueError(f"There is already a clave called '{name}'.")
            clave.name = name
        if members is not None:
            clave.members = _check_members(members)
        if lead is not _UNSET:
            if lead and lead not in clave.members:
                raise ValueError("the lead must be a member of the clave")
            clave.lead = lead or None
        elif clave.lead not in clave.members:
            clave.lead = None
        _write(clave)
    return clave.as_dict()


def archive(clave_id: str) -> None:
    """Take a clave away: its folder moves to Claves/_Archived (nothing is
    deleted; the agents themselves are untouched)."""
    with _LOCK:
        clave = get(clave_id)
        target_dir = root() / ARCHIVE
        target_dir.mkdir(exist_ok=True)
        target = target_dir / clave.folder
        n = 2
        while target.exists():
            target, n = target_dir / f"{clave.folder} {n}", n + 1
        shutil.move(str(root() / clave.folder), str(target))


def set_agent_clave(agent_id: str, clave_id: str | None) -> str | None:
    """The phone's picker: move the agent into this clave (None = no clave)."""
    if agent_id not in AGENTS:
        raise KeyError(agent_id)
    if agent_id == _manager():
        raise ValueError("The manager is in every clave.")
    with _LOCK:
        if clave_id:
            place(agent_id, clave_id)
        else:
            _leave_others(agent_id, None)
    return clave_of(agent_id)


def place(agent_id: str, to: str, *, index: int | None = None, lead: bool = False) -> list[dict]:
    """Drag and drop in the overview: move an agent into clave `to` (at
    `index`, or as its lead), out of any other clave."""
    if agent_id not in AGENTS:
        raise KeyError(agent_id)
    with _LOCK:
        target = get(to)
        if agent_id != _manager() and name_taken_in_clave(AGENTS[agent_id].name, to, exclude=agent_id):
            raise ValueError(f"{target.name} already has an agent called {AGENTS[agent_id].name}.")
        _leave_others(agent_id, to)
        target = get(to)
        rest = [a for a in target.members if a != agent_id]
        index = len(rest) if index is None else max(0, min(index, len(rest)))
        target.members = rest[:index] + [agent_id] + rest[index:]
        if lead:
            target.lead = agent_id
        elif target.lead == agent_id:
            target.lead = None
        _write(target)
    return overview()["claves"]


def remove(agent_id: str, clave_id: str) -> list[dict]:
    with _LOCK:
        clave = get(clave_id)
        clave.members = [a for a in clave.members if a != agent_id]
        if clave.lead == agent_id:
            clave.lead = None
        _write(clave)
    return overview()["claves"]


def ensure_default(order: list[str]) -> None:
    """Turning claves on for the first time: one "Personal" clave with every
    agent, so nobody is left out (the manager is in every clave anyway)."""
    with _LOCK:
        if not list_claves():
            create("Personal", members=[a for a in order if a in AGENTS])


def active() -> str | None:
    """The clave picked next to the Claves button on the home page: the
    Dashboard button, Boxes and an agent page's tabs show its agents. None
    while claves are off; the first clave when none (or a gone one) is picked."""
    if not enabled():
        return None
    ids = [c.id for c in list_claves()]
    if not ids:
        return None
    chosen = service_settings.load().get("active_clave") or ""
    return chosen if chosen in ids else ids[0]


def set_active(clave_id: str) -> None:
    get(clave_id)  # KeyError for an unknown clave
    service_settings.update({"active_clave": clave_id})


def visible(order: list[str]) -> list[str]:
    """`order` limited to the active clave's members (all of it with no clave active)."""
    clave_id = active()
    if not clave_id:
        return list(order)
    present = set(members(clave_id))  # includes the manager
    return [a for a in order if a in present]


def overview() -> dict:
    import agents as agents_mod
    return {"enabled": enabled(), "manager": agents_mod.manager_id(), "active": active(),
            "claves": [c.as_dict() for c in list_claves()]}


def clave_context(clave_id: str) -> str:
    """One line for an agent answering in a clave's room: which clave, who's in it."""
    if clave_id == EVERYONE:
        return ""
    clave = get(clave_id).as_dict()
    names = ", ".join(AGENTS[a].name for a in clave["members"])
    lead = f" {AGENTS[clave['lead']].name} leads it." if clave["lead"] else ""
    return f"This is the {clave['name']} clave's group chat (members: {names}).{lead}"


# ── The clave's backlog and its lead (2026-11-05) ────────────────────────
# Each clave folder has backlog.md: shared work for its members. Any member
# (and the manager, who is in every clave) can read it and add items; only
# the lead or the manager ticks them off. The lead is told it owns the
# backlog and coordinates; members are told who leads.

def claves_of(agent_id: str) -> list[Clave]:
    """The claves an agent works in: its own, plus any it leads (the manager
    can lead several)."""
    if not enabled():
        return []
    return [c for c in list_claves() if agent_id in c.as_dict()["members"] or c.lead == agent_id]


def backlog_path(clave: Clave):
    return root() / clave.folder / "backlog.md"


def backlog_tool(agent_id: str, action: str, item: str = "", clave_id: str = "") -> str:
    import vault
    mine = claves_of(agent_id)
    if not mine:
        return "refused: you're not in a clave (or claves are off)"
    clave = next((c for c in mine if c.id == clave_id), None) if clave_id else (mine[0] if len(mine) == 1 else None)
    if not clave:
        return f"say which clave: {', '.join(c.id for c in mine)}"
    path = backlog_path(clave)
    with _LOCK:
        text = path.read_text(encoding="utf-8") if path.exists() else f"---\nUpdated: {date.today()}\n---\n\n# {clave.name} — Backlog\n\n"
        fm, body = vault.split_frontmatter(text)
        if action == "read":
            return body.strip() or "(empty)"
        item = " ".join((item or "").split())
        if not item:
            return "refused: give the item text"
        if action == "add":
            body = body.rstrip("\n") + f"\n- [ ] {item[:300]}\n"
        elif action in ("check", "uncheck"):
            if clave.lead in AGENTS and agent_id not in (clave.lead, _manager()):
                return f"refused: only the clave's lead ({AGENTS[clave.lead].name}) ticks items off"
            lines, hit = body.split("\n"), False
            for i, line in enumerate(lines):
                m = re.match(r"^- \[[ x]\] (.*)$", line)
                if m and not hit and item.lower() in m.group(1).lower():
                    lines[i] = f"- [{'x' if action == 'check' else ' '}] {m.group(1)}"
                    hit = True
            if not hit:
                return "no matching item"
            body = "\n".join(lines)
        else:
            return f"unknown action: {action}"
        fm["Updated"] = str(date.today())
        path.write_text(vault.render_frontmatter(fm, body), encoding="utf-8")
    return "Success!"


def prompt_line(agent_id: str) -> str:
    lines = []
    for clave in claves_of(agent_id):
        d = clave.as_dict()
        names = ", ".join(AGENTS[a].name for a in d["members"] if a != agent_id)
        if d["lead"] == agent_id:
            lines.append(f"You lead the {clave.name} clave (with {names or 'no one else yet'}). Keep its shared backlog "
                         f"current with clave_backlog (clave '{clave.id}'): break shared work into items, tick off what's "
                         "done, and coordinate the members with message_agent or the clave's group chat. Members bring "
                         "work that spans the clave to you.")
        else:
            lead = AGENTS[d["lead"]].name if d["lead"] in AGENTS else None
            lines.append(f"You're in the {clave.name} clave (with {names or 'no one else'})"
                         + (f", led by {lead}: take work that spans the clave to them" if lead else "")
                         + f". Its shared backlog: clave_backlog (clave '{clave.id}'; read it, add items"
                         + ("; the lead ticks them off)." if lead else ")."))
    return "\n".join(lines) + "\n" if lines else ""
