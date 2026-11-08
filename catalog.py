"""Ready-made agents (BACKLOG T3): templates a user installs instead of
starting from a blank agent.

A template is a folder with ``agent.json`` (name, colour, tagline, any
connections/roles, and a ``catalog`` block: summary, category, suggested
tools, what it needs), ``mission.md`` and ``persona.md`` (bodies, written
about "the user"), ``onboarding.md`` (questions that fit the template to a
new user), and optionally ``avatar.svg`` and ``skills/``.

The built-in catalog is ``catalog/agents`` in this repo; ``[catalog]
sources`` in the config adds more folders, e.g. a community catalog cloned
with git. Anyone can contribute a template with a pull request; see
catalog/README.md.

An installed agent starts in onboarding like a blank one, but its mission
and persona are already written: the interview fits them to the user
(agent_factory.onboarding_directive reads the template's questions from
agent.json) instead of writing them from scratch.
"""
import json
import re
import shutil
from pathlib import Path

import agent_factory
import agents
import llm_models
from config import CONFIG

BUILTIN = Path(__file__).parent / "catalog" / "agents"
REQUIRED = ("agent.json", "mission.md", "persona.md")
CARRIED = ("roles", "connections", "mcp_servers", "cli_tools", "voice")


def roots() -> list[tuple[str, Path]]:
    out = [("built-in", BUILTIN)]
    out += [(str(p), p / "agents" if (p / "agents").is_dir() else p) for p in CONFIG.catalog_sources]
    return out


def _questions(folder: Path) -> list[str]:
    f = folder / "onboarding.md"
    if not f.is_file():
        return []
    return [m.group(1).strip() for m in re.finditer(r"(?m)^\s*-\s+(.+)$", f.read_text(encoding="utf-8"))]


def _read(folder: Path, source: str) -> dict | None:
    if not all((folder / f).is_file() for f in REQUIRED):
        return None
    try:
        data = json.loads((folder / "agent.json").read_text(encoding="utf-8"))
    except ValueError:
        return None
    meta = data.get("catalog") if isinstance(data.get("catalog"), dict) else {}
    if not all(isinstance(data.get(k), str) and data[k] for k in ("id", "name", "tagline")):
        return None
    return {
        "id": data["id"], "name": data["name"], "tagline": data["tagline"], "color": data.get("color", "#888888"),
        "summary": meta.get("summary", ""), "category": meta.get("category", "Other"),
        "needs": meta.get("needs", ""), "suggested_tools": meta.get("suggested_tools"),
        "version": meta.get("version", 1), "authors": meta.get("authors", []),
        "has_avatar": (folder / "avatar.svg").is_file(),
        "skills": sorted(p.name for p in (folder / "skills").iterdir() if (p / "SKILL.md").is_file())
        if (folder / "skills").is_dir() else [],
        "source": source, "folder": folder, "data": data,
    }


def templates() -> list[dict]:
    """Every template, first source wins on duplicate ids."""
    seen: dict[str, dict] = {}
    for source, root in roots():
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir()):
            t = _read(folder, source) if folder.is_dir() else None
            if t and t["id"] not in seen:
                seen[t["id"]] = t
    return list(seen.values())


def get(template_id: str) -> dict:
    t = next((t for t in templates() if t["id"] == template_id), None)
    if t is None:
        raise ValueError(f"no ready-made agent called {template_id!r}")
    return t


def installed_from() -> dict[str, list[str]]:
    """template id -> ids of agents installed from it."""
    out: dict[str, list[str]] = {}
    for cfg in agents.AGENTS.values():
        try:
            data = json.loads((agents.AGENTS_ROOT / cfg.folder / agents.AGENT_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        template = data.get("template") if isinstance(data.get("template"), dict) else {}
        if template.get("id"):
            out.setdefault(template["id"], []).append(cfg.id)
    return out


def public(t: dict) -> dict:
    return {k: v for k, v in t.items() if k not in ("folder", "data")}


def listing() -> list[dict]:
    used = installed_from()
    return [{**public(t), "installed": used.get(t["id"], [])} for t in templates()]


def install(template_id: str, model_id: str, name: str | None = None) -> dict:
    t = get(template_id)
    name = (name or t["name"]).strip()
    agent_id = agent_factory._check_new_name(name)
    llm_models.get(model_id)  # raises on an unknown model
    questions = _questions(t["folder"])
    agent_data = {k: t["data"][k] for k in CARRIED if k in t["data"]}
    agent_data["template"] = {"id": t["id"], "version": t["version"], "source": t["source"], "questions": questions}
    suggested = t["suggested_tools"]
    import agent_tools
    tools = list(agent_tools.optional_for(agent_id)) if suggested is None else list(suggested)
    color = t["color"] if t["color"] not in {a.color for a in agents.AGENTS.values()} else agent_factory._next_color()
    first = questions[0] if questions else "What do you want from me, and what does a good outcome look like?"
    created = agent_factory._scaffold(
        name, agent_id, color=color, tagline=t["tagline"],
        mission=_body(t["folder"] / "mission.md", t["name"], name),
        persona=_body(t["folder"] / "persona.md", t["name"], name),
        agent_data=agent_data, model_id=model_id, tools=tools,
        onboarding_item="Onboarding interview: fit the ready-made mission and persona to the user",
        opening=(f"Hi, I'm {name}. I come ready-made, so my job and personality are already written, just not "
                 f"for you yet. A few quick questions, one at a time, so I fit you.\n\n{first}"))
    folder = created["folder"]
    if t["has_avatar"]:
        shutil.copyfile(t["folder"] / "avatar.svg", folder / "avatar.svg")
    if (t["folder"] / "skills").is_dir():
        shutil.copytree(t["folder"] / "skills", folder / "skills")
    return {"id": created["id"], "conversation_id": created["conversation_id"]}


def _body(path: Path, template_name: str, name: str) -> str:
    """The template text, with the agent's name if the user renamed it."""
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        text = text[end + 4:] if end != -1 else text
    return re.sub(rf"\b{re.escape(template_name)}\b", name, text) if name != template_name else text
