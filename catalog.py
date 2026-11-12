"""Ready-made agents: templates a user installs instead of
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


def _options(folder: Path, sub: str = "personas") -> list[dict]:
    """Suggestions to choose from, one .md each with front matter Label,
    Summary, Order: personas/ (starting personalities) and extras/ (optional
    extra jobs, added to the mission when picked). The manager template has both."""
    out = []
    for f in sorted((folder / sub).glob("*.md")) if (folder / sub).is_dir() else []:
        text = f.read_text(encoding="utf-8")
        meta = dict(re.findall(r"(?m)^(\w+):\s*(.+)$", text[:text.find("\n---", 3)])) if text.startswith("---") else {}
        out.append({"id": f.stem, "label": meta.get("Label", f.stem), "summary": meta.get("Summary", ""),
                    "order": int(meta.get("Order", 99)) if str(meta.get("Order", "")).isdigit() else 99, "path": f})
    return sorted(out, key=lambda p: (p["order"], p["id"]))


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
        "personas": _options(folder, "personas"),
        "extras": _options(folder, "extras"),
        "job": [x for x in meta.get("job", []) if isinstance(x, str)],
        "name_ideas": [x for x in meta.get("name_ideas", []) if isinstance(x, str)],
        # The manager has no default name: the user names it (install.sh, or the name box).
        "manager": agents.MANAGER in (data.get("roles") or []) or agents.LEGACY_MANAGER in (data.get("roles") or []),
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
    out = {k: v for k, v in t.items() if k not in ("folder", "data")}
    for key in ("personas", "extras"):
        out[key] = [{k: p[k] for k in ("id", "label", "summary")} for p in t[key]]
    return out


def listing() -> list[dict]:
    used = installed_from()
    return [{**public(t), "installed": used.get(t["id"], [])} for t in templates()]


# The user's own words: short enough that the files stay within the
# self-edit limits (vault.IDENTITY_CHAR_LIMITS) with every extra job picked.
PERSONA_MAX, MISSION_EXTRA_MAX = 1000, 300


def install(template_id: str, model_id: str, name: str | None = None,
            persona: str | None = None, mission_extra: str | None = None,
            extras: list[str] | None = None) -> dict:
    """persona: a starting personality's id (template's personas/), or the
    user's own description. extras: ids of optional extra jobs (extras/),
    none unless picked. mission_extra: the user's changes to the job.
    The manager needs a name and a persona from the user: no defaults."""
    t = get(template_id)
    persona, mission_extra = (persona or "").strip(), (mission_extra or "").strip()
    if t["manager"] and not (name or "").strip():
        raise ValueError("Give your manager a name first.")
    current = [agents.AGENTS[i].name for i in agents.with_role(agents.MANAGER)]
    if t["manager"] and current:
        raise ValueError(f"You already have a manager: {current[0]}. OpenClave works with exactly one.")
    if t["manager"] and not persona:
        raise ValueError("Choose your manager's personality, or describe it in your own words.")
    chosen = next((p for p in t["personas"] if p["id"] == persona), None)
    if persona and not chosen and len(persona) < 10:
        raise ValueError("Describe the personality in a few more words.")
    if len(persona) > PERSONA_MAX:
        raise ValueError(f"Keep the personality under {PERSONA_MAX} characters.")
    if len(mission_extra) > MISSION_EXTRA_MAX:
        raise ValueError(f"Keep the job changes under {MISSION_EXTRA_MAX} characters.")
    picked = []
    for x in extras or []:
        extra = next((e for e in t["extras"] if e["id"] == x), None)
        if extra is None:
            raise ValueError(f"No extra job called {x!r}.")
        if extra not in picked:
            picked.append(extra)
    name = (name or t["name"]).strip()
    agent_id = agent_factory._check_new_name(name)
    llm_models.get(model_id)  # raises on an unknown model
    questions = [_renamed(q, t["name"], name) for q in _questions(t["folder"])]
    agent_data = {k: t["data"][k] for k in CARRIED if k in t["data"]}
    agent_data["template"] = {"id": t["id"], "version": t["version"], "source": t["source"], "questions": questions}
    suggested = t["suggested_tools"]
    import agent_tools
    tools = list(agent_tools.optional_for(agent_id)) if suggested is None else list(suggested)
    color = t["color"] if t["color"] not in {a.color for a in agents.AGENTS.values()} else agent_factory._next_color()
    first = questions[0] if questions else "What do you want from me, and what does a good outcome look like?"
    created = agent_factory._scaffold(
        name, agent_id, color=color, tagline=t["tagline"],
        mission=("\n\n".join([_body(t["folder"] / "mission.md", t["name"], name).strip()]
                             + [_body(e["path"], t["name"], name).strip() for e in picked]) + "\n"
                 + _mission_changes(mission_extra)),
        persona=(_body(chosen["path"], t["name"], name) if chosen
                 else _own_persona(name, persona) if persona
                 else _body(t["folder"] / "persona.md", t["name"], name)),
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
    return _renamed(text, template_name, name)


def _own_persona(name: str, description: str) -> str:
    return (f"# {name} — Persona\n\nThe user described the personality they want, in their own words:\n\n"
            + "\n".join(f"> {line}" for line in description.splitlines()) +
            "\n\nBe exactly that in every reply, without letting it get in the way of the content. In the "
            "onboarding interview, ask about anything the description leaves open, then rewrite this file "
            "as a proper persona (who you are, how you talk) with update_persona.\n\n## Feedback\n")


def _mission_changes(text: str) -> str:
    if not text:
        return ""
    return ("\n\n## The user's changes\n\nIn their own words; this wins over anything above. Fold it in "
            "during onboarding (update_mission).\n\n" + "\n".join(f"> {line}" for line in text.splitlines()) + "\n")


def _renamed(text: str, template_name: str, name: str) -> str:
    return re.sub(rf"\b{re.escape(template_name)}\b", name, text) if name != template_name else text


# ── Avatar library: pictures any agent can pick (catalog/avatars/*.svg) ──
AVATARS = Path(__file__).parent / "catalog" / "avatars"


def avatar_library() -> list[dict]:
    """[{id, label}]; the label is the SVG's <title> or aria-label, else the file name."""
    out = []
    for f in sorted(AVATARS.glob("*.svg")):
        head = f.read_text(encoding="utf-8")[:600]
        m = re.search(r"<title>([^<]+)</title>", head) or re.search(r'aria-label="([^"]+)"', head)
        out.append({"id": f.stem, "label": m.group(1).strip() if m else f.stem.replace("-", " ").capitalize()})
    return out


def library_avatar(avatar_id: str) -> str:
    """The SVG text of one library picture."""
    if avatar_id not in {a["id"] for a in avatar_library()}:
        raise ValueError(f"No picture called {avatar_id!r} in the avatar library.")
    return (AVATARS / f"{avatar_id}.svg").read_text(encoding="utf-8")
