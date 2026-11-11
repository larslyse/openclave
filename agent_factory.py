"""Creating a new agent from the Control Panel, and finishing its onboarding.

Setup is deliberately tiny (name, what it's for, model). Everything else — the
real mission, persona and tool set — comes out of an onboarding interview the
agent runs with the user itself (see ONBOARDING_DIRECTIVE), ending in a
finish_onboarding tool call.
"""
import json
import os
import re
import shutil
from dataclasses import replace
from datetime import date

import agent_tools
import avatar_maker
import agents
from config import CONFIG
from state_io import save_json
import llm_client
import llm_models
import svg_avatar
import vault
from PIL import Image
from pathlib import Path

TEMPLATE_DASHBOARD = Path(__file__).parent / "templates" / "agent_dashboard.html"
AVATAR_DIR = CONFIG.state_path("custom_avatars")
RESERVED_IDS = {"api", "static", "mobile", "control", "docs", "openapi", "redoc"}
PALETTE = ["#3DB8C4", "#C4A23D", "#6FC43D", "#C43D6F", "#3D6FC4", "#A23DC4", "#C4763D", "#3DC48B"]
TAGLINE_MAX = 60


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _initials(name: str) -> str:
    return (re.sub(r"[^A-Za-z0-9]", "", name)[:2] or "AG").upper()


def _check_new_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 \-]{0,23}", name):
        raise ValueError("Name: 1-24 letters, digits, spaces or dashes.")
    agent_id = _slug(name)
    if not agent_id or agent_id in RESERVED_IDS or agent_id in agents.AGENTS:
        raise ValueError(f"The name '{name}' is taken or reserved.")
    if (agents.AGENTS_ROOT / name).exists():
        raise ValueError(f"A folder named '{name}' already exists in the vault.")
    return agent_id


def _frontmatter(fields: dict) -> str:
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items()) + "---\n\n"


def _scaffold(name: str, agent_id: str, *, color: str, tagline: str, mission: str, persona: str,
              agent_data: dict, model_id: str, tools: list[str], opening: str, onboarding_item: str) -> dict:
    """Create the agent folder and register the agent, in onboarding. Shared
    by a blank new agent and one installed from the catalog."""
    today = str(date.today())
    folder = agents.AGENTS_ROOT / name
    folder.mkdir(parents=True)
    (folder / "conversations").mkdir()
    (folder / "memory").mkdir()
    (folder / "mission.md").write_text(_frontmatter({"Agent": name, "Role": json.dumps(tagline), "Created": today,
                                                     "Updated": today, "Status": "onboarding"}) + mission.strip() + "\n",
                                       encoding="utf-8")
    (folder / "persona.md").write_text(_frontmatter({"Agent": name, "Created": today, "Updated": today,
                                                     "Status": "onboarding"}) + persona.strip() + "\n", encoding="utf-8")
    (folder / "backlog.md").write_text(f"""---
Updated: {today}
---

# {name} — Backlog

- [ ] {onboarding_item}
""", encoding="utf-8")
    (folder / "plan.md").write_text(f"""---
Status: active
Updated: {today}
Target-event: ""
Phases:
- name: Onboarding
  status: in-progress
  percent: 0
---

# {name} — Current Plan

Just created. First step is the onboarding interview.
""", encoding="utf-8")
    (folder / "dashboard.html").write_text(render_dashboard(name, agent_id, color, _initials(name)), encoding="utf-8")
    data = {"id": agent_id, "name": name, "color": color, "tagline": tagline, **agent_data,
            "onboarding": True, "created": today}
    save_json(folder / agents.AGENT_FILE, data, ensure_ascii=False)

    agents.register(agents._agent_config(data, name))
    llm_models.set_selected_model(agent_id, model_id)
    agent_tools.set_optional(agent_id, [t for t in tools if t in agent_tools.optional_for(agent_id)])

    conversation_id = vault.new_conversation_id()
    vault.append_turn(agent_id, conversation_id, "assistant", opening, first_message="Onboarding")
    # Remembered so the weekly cleanup can offer to delete it once onboarding
    # is done: mission, persona and tools hold what came out of it.
    data["onboarding_conversation"] = conversation_id
    save_json(folder / agents.AGENT_FILE, data, ensure_ascii=False)
    return {"id": agent_id, "conversation_id": conversation_id, "folder": folder}


def render_dashboard(name: str, agent_id: str, color: str, initials: str) -> str:
    dashboard = TEMPLATE_DASHBOARD.read_text(encoding="utf-8")
    for key, value in {"NAME": name, "ID": agent_id, "COLOR": color, "INITIALS": initials}.items():
        dashboard = dashboard.replace("{{" + key + "}}", value)
    return dashboard


# ── Upgrading the agents' page copies (2026-11-05) ────────────────────────
# Each agent's dashboard.html starts as a copy of templates/agent_dashboard.html
# filled in with its name, id, colour and initials; an agent may rewrite its
# own (write_own_dashboard). A copy is "stock" when every line of it, with the
# agent's values and colours blanked, is a line of the template (or one of the
# lines older templates had, below). Stock copies are re-rendered from the
# current template at startup, so template fixes reach every agent; anything
# an agent wrote makes the copy custom, and it's never touched.
_LEGACY_LINES = {
    '<div class="usage-line" id="usage-line"></div>',
    '<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>',
    '<script src="https://cdn.jsdelivr.net/npm/dompurify/dist/purify.min.js"></script>',
}


def _norm_lines(text: str, values: dict) -> set[str]:
    out = set()
    for line in text.splitlines():
        line = line.strip()
        for key, value in values.items():
            if value:
                line = line.replace(value, "{{" + key + "}}")
        line = re.sub(r"#[0-9A-Fa-f]{6}\b", "#C", line)
        line = re.sub(r"\{\{COLOR\}\}", "#C", line)
        line = re.sub(r'<div class="icon">[^<]*</div>', '<div class="icon">{{INITIALS}}</div>', line)
        if line:
            out.add(line)
    return out


def dashboard_is_stock(text: str, cfg) -> bool:
    values = {"NAME": cfg.name, "ID": cfg.id}
    allowed = _norm_lines(TEMPLATE_DASHBOARD.read_text(encoding="utf-8"), {}) | _LEGACY_LINES
    return _norm_lines(text, values) <= allowed


def upgrade_stock_dashboards() -> list[str]:
    """Re-render stock copies that differ from the current template. Returns
    the agent ids upgraded (each old copy is kept as dashboard.html.bak)."""
    done = []
    for cfg in list(agents.AGENTS.values()):
        f = agents.AGENTS_ROOT / cfg.folder / "dashboard.html"
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if not dashboard_is_stock(text, cfg):
            continue
        initials = (re.search(r'<div class="icon">([^<]*)</div>', text) or [None, _initials(cfg.name)])[1]
        fresh = render_dashboard(cfg.name, cfg.id, cfg.color, initials)
        if fresh != text:
            f.with_name("dashboard.html.bak").write_text(text, encoding="utf-8")
            f.write_text(fresh, encoding="utf-8")
            done.append(cfg.id)
    return done


def _next_color() -> str:
    used = {a.color for a in agents.AGENTS.values()}
    return next((c for c in PALETTE if c not in used), PALETTE[len(agents.AGENTS) % len(PALETTE)])


def create_agent(name: str, purpose: str, model_id: str) -> dict:
    name, purpose = name.strip(), " ".join(purpose.split())
    if not purpose:
        raise ValueError("Say briefly what the agent is for.")
    agent_id = _check_new_name(name)
    llm_models.get(model_id)  # raises on an unknown model
    tagline = purpose if len(purpose) <= TAGLINE_MAX else purpose[:TAGLINE_MAX - 1].rstrip() + "…"
    created = _scaffold(
        name, agent_id, color=_next_color(), tagline=tagline,
        mission=f"# Goals / objectives\n\n- {purpose}\n\n## Domains\n\n- {purpose}\n",
        persona=f"# {name} — Persona\n\nNot set yet: shaped during the onboarding interview.\n\n## Feedback\n",
        agent_data={}, model_id=model_id, tools=[],  # core only until onboarding picks more
        onboarding_item="Onboarding interview: mission, persona and tools",
        opening=(f"Hi, I'm {name}. Before I get going, a few quick questions so I can set myself up properly. "
                 "One at a time, a couple of minutes.\n\n"
                 f"You said I'm for: \"{purpose}\". What would make me genuinely useful there, "
                 "and what does a good outcome look like?"))
    return {"id": created["id"], "conversation_id": created["conversation_id"]}


def save_svg_avatar(agent_id: str, svg: str) -> None:
    """Sanitized SVG replaces any uploaded PNG (the avatar endpoint serves whichever exists)."""
    clean = svg_avatar.sanitize(svg)
    AVATAR_DIR.mkdir(exist_ok=True)
    (AVATAR_DIR / f"{agent_id}.svg").write_text(clean, encoding="utf-8")
    (AVATAR_DIR / f"{agent_id}.png").unlink(missing_ok=True)
    (AVATAR_DIR / f"{agent_id}.base.png").unlink(missing_ok=True)


def finish_onboarding(agent_id: str, tools: list[str], tagline: str | None, avatar_svg: str | None = None,
                      topics: list[str] | None = None) -> str:
    if not agents.is_onboarding(agent_id):
        return "refused: onboarding is already finished"
    valid = [t for t in tools if t in agent_tools.optional_for(agent_id)]
    ignored = sorted(set(tools) - set(valid) - set(agent_tools.CORE_TOOLS))
    agent_tools.set_optional(agent_id, valid)

    cfg = agents.AGENTS[agent_id]
    agent_file = agents.AGENTS_ROOT / cfg.folder / agents.AGENT_FILE
    data = json.loads(agent_file.read_text(encoding="utf-8"))
    if tagline and tagline.strip():
        data["tagline"] = tagline.strip()[:TAGLINE_MAX]
        agents.register(replace(cfg, tagline=data["tagline"]))
    data["onboarding"] = False
    save_json(agent_file, data)

    for f in (vault.mission_file(agent_id), vault.persona_file(agent_id)):
        text = f.read_text(encoding="utf-8")
        f.write_text(text.replace("Status: onboarding", "Status: active", 1), encoding="utf-8")
    vault.backlog_set_done(agent_id, "Onboarding interview", done=True)

    note = f" Ignored (unknown or core): {', '.join(ignored)}." if ignored else ""
    if avatar_svg and avatar_svg.strip():
        # A bad drawing must never block finishing onboarding — report it instead.
        try:
            save_svg_avatar(agent_id, avatar_svg)
            note += " Avatar saved."
        except svg_avatar.InvalidSvg as exc:
            note += f" Avatar not saved ({exc}); the user can upload one in the Control Panel."
    made = _create_topics(agent_id, topics or [])
    if made:
        note += f" Topics: {', '.join(made)}."
    return f"Onboarding finished. Optional tools: {', '.join(valid) or 'none'}.{note}"


MAX_ONBOARDING_TOPICS = 12


def _create_topics(agent_id: str, names: list[str]) -> list[str]:
    """Chat topics (folders.py) agreed in the interview, usually one per
    domain. Existing ones and bad names are skipped, never an error."""
    import folders
    made = []
    for name in [n for n in names if isinstance(n, str)][:MAX_ONBOARDING_TOPICS]:
        try:
            made.append(folders.create_folder(agent_id, name)["name"])
        except (ValueError, FileExistsError):
            continue
    return made


# The onboarding step that offers chat topics (both interviews).
_TOPICS_STEP = ("Offer chat topics: chats are sorted into topics (plain folders in the chat list), and your "
                "domains are the natural ones, since each is a thread the user will keep coming back to. "
                "From your '## Domains' list, propose 3-8 short topic names (merge tiny domains, split none), "
                "say in one sentence why, and ask: \"Should I create these topics for you?\" "
                "Use the names they agree to (they can add, rename or skip; topics can always be changed later).")


def onboarding_directive(agent_id: str) -> str:
    catalog = "\n".join(
        f"- {t['function']['name']}: {t['function']['description'].split('. ')[0].rstrip('.')}."
        for t in llm_client.TOOLS if t["function"]["name"] in agent_tools.optional_for(agent_id)
    ) + "".join(f"\n- {name}: {text.split('. ')[0]}." for name, text in agent_tools.CAPABILITIES.items())
    template = _template_info(agent_id)
    if template is not None:
        return _template_directive(agent_id, template, catalog)
    return f"""# Onboarding (you were just created)
You are new, and your only job right now is a short setup interview with the user. No domain work yet.
1. Ask one short question at a time, 3-5 in total before your proposal. Cover: what they want from you and what good looks like; what you should keep track of (search the vault for existing notes on the topic and mention what you find); where your lane ends versus the other agents in the roster; your personality and tone.
2. Decide which optional tools the job actually needs, from the list below. Fewer is better.
3. Propose in one message: your mission (goals plus a '## Domains' bullet list, at most 1,850 characters), your persona (who you are, tone, how you work, at most 2,200 characters, ending with an empty '## Feedback' section), and your tool picks with a few words on why. Ask the user to confirm or adjust.
4. Optionally, offer to draw your own avatar: ask what it should look like, and include it in the proposal as a small SVG (viewBox 0 0 64 64, a few plain shapes, no text needed).
5. Offer a voice for voice mode: ask what you should sound like, call find_voice, and give them 2-3 options, each with its sample link so they can listen. Call set_voice with the one they pick (they can also skip this).
6. Ask what they'd like on your phone page (check_page shows what a page can hold). If they have nothing specific, say you'll build a simple one from the files you already have (your plan and backlog, plus any note or log that matters for your job), and do that.
7. {_TOPICS_STEP}
8. Once they confirm: write mission.md and persona.md in full (keep their frontmatter), then call finish_onboarding with the agreed tools (and avatar_svg if they wanted one, and topics with the agreed topic names). Then write your phone page (check_page first, then write_page) and tell them you're set up.

Core tools you always have: {', '.join(agent_tools.CORE_TOOLS)}.
Optional tools to choose from:
{catalog}"""


def _template_info(agent_id: str) -> dict | None:
    cfg = agents.AGENTS[agent_id]
    try:
        data = json.loads((agents.AGENTS_ROOT / cfg.folder / agents.AGENT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("template") if isinstance(data.get("template"), dict) else None


def _template_directive(agent_id: str, template: dict, catalog: str) -> str:
    """Onboarding for an agent installed from the catalog (catalog.py): its
    mission and persona exist; the interview fits them to this user."""
    questions = "\n".join(f"   - {q}" for q in template.get("questions") or []) or "   - What do you want from me?"
    selected = agent_tools.selected_optional(agent_id) or []
    return f"""# Onboarding (you were just installed from a ready-made template)
Your mission and persona are already written: read them first (read_vault_file). They describe the job and the character people liked, but they were written for "the user" in general. Your only job right now is a short interview that fits them to this user. No domain work yet.
1. Ask one short question at a time. Start with these template questions (skip what the user already answered, follow up where an answer matters):
{questions}
   Also search the vault for notes the user already has on your topic and mention what you find.
2. Check your personality with the user: describe it in two sentences and ask if it fits or what to change. Keep it unless they want changes.
3. Tools: you start with these optional tools, picked for this template: {', '.join(selected) or 'none'}. Change them only if the answers call for it.
4. Propose in one message: your mission with what you learned added (keep the template's goals and domains unless the user changes them; add a short '## About the user' section with the facts that matter for your job; at most 1,850 characters), your persona (unchanged or adjusted, at most 2,200 characters, ending with a '## Feedback' section), and your tool picks. Ask the user to confirm or adjust.
5. Offer a voice for voice mode: ask what you should sound like, call find_voice, give 2-3 options with sample links, and call set_voice with their pick (they can skip this). You already have an avatar if the template had one; offer to draw a new one only if asked.
6. Ask what they'd like on your phone page (check_page shows what a page can hold). If nothing specific, build a simple one from your plan, backlog and the main note or log of your job.
7. {_TOPICS_STEP}
8. Once they confirm: write mission.md and persona.md in full (keep their frontmatter), then call finish_onboarding with the agreed tools (and topics with the agreed topic names). Then write your phone page and tell them you're set up.

Core tools you always have: {', '.join(agent_tools.CORE_TOOLS)}.
Optional tools to choose from:
{catalog}"""


def rename_agent(agent_id: str, name: str, tagline: str) -> dict:
    """Display name and tagline only: the id (URLs), vault folder and history
    stay. The old name is kept in former_names so transcripts labelled with it
    still parse. Built-in agents get an agent.json holding just these overrides."""
    cfg = agents.AGENTS[agent_id]
    name, tagline = name.strip(), " ".join(tagline.split())
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 \-]{0,23}", name):
        raise ValueError("Name: 1-24 letters, digits, spaces or dashes.")
    # Clones share a name across claves (claves.py); within a clave names differ.
    import claves
    clave_id = claves.clave_of(agent_id) if claves.enabled() else None
    if clave_id or agent_id == agents.manager_id() and claves.enabled():
        taken = any(claves.name_taken_in_clave(name, c, exclude=agent_id)
                    for c in ([clave_id] if clave_id else [c.id for c in claves.list_claves()]))
    else:
        taken = any(a.id != agent_id and a.name.lower() == name.lower() for a in agents.AGENTS.values())
    if taken:
        raise ValueError(f"Another agent is already called {name}.")
    if not tagline or len(tagline) > TAGLINE_MAX:
        raise ValueError(f"Tagline: 1-{TAGLINE_MAX} characters.")

    agent_file = agents.AGENTS_ROOT / cfg.folder / agents.AGENT_FILE
    data = json.loads(agent_file.read_text(encoding="utf-8")) if agent_file.exists() else {"id": agent_id}
    former = [n for n in data.get("former_names", list(cfg.former_names)) if n != name]
    if name != cfg.name and cfg.name not in former:
        former.append(cfg.name)
    data.update(name=name, tagline=tagline, former_names=former)
    save_json(agent_file, data)
    agents.register(replace(cfg, name=name, tagline=tagline, former_names=tuple(former)))

    if name != cfg.name:
        _rename_in_dashboard(cfg, name)
    return {"id": agent_id, "name": name, "tagline": tagline}


def set_color(agent_id: str, color: str) -> dict:
    """The agent's colour (avatar background, tile border, its page's accent,
    chat folder names). Saved in agent.json; the agent's own page has it baked
    into its :root block (templates/agent_dashboard.html), so that's updated too."""
    cfg = agents.AGENTS[agent_id]
    color = color.strip()
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        raise ValueError("Colour: #RRGGBB.")
    agent_file = agents.AGENTS_ROOT / cfg.folder / agents.AGENT_FILE
    data = json.loads(agent_file.read_text(encoding="utf-8")) if agent_file.exists() else {"id": agent_id}
    data["color"] = color
    save_json(agent_file, data)
    agents.register(replace(cfg, color=color))
    f = agents.AGENTS_ROOT / cfg.folder / "dashboard.html"
    if f.exists() and cfg.color != color:
        text = f.read_text(encoding="utf-8")
        for var in ("--accent", "--accent-dark", "--flagged"):
            text = re.sub(rf"({re.escape(var)}:\s*){re.escape(cfg.color)}(\s*;)", rf"\g<1>{color}\2", text, flags=re.I)
        f.write_text(text, encoding="utf-8")
    _retint_avatar(agent_id, color)
    return {"id": agent_id, "color": color}


def _retint_avatar(agent_id: str, color: str) -> None:
    """A picture avatar saved with the agent-colour tint follows the colour:
    re-tint the kept untinted picture (main.set_agent_avatar)."""
    base = AVATAR_DIR / f"{agent_id}.base.png"
    if not base.exists():
        return
    with Image.open(base) as image:
        tinted = avatar_maker.tint(image.convert("RGBA"), color)
    tmp = AVATAR_DIR / f".{agent_id}.png.tmp"
    tinted.save(tmp, format="PNG")
    os.replace(tmp, AVATAR_DIR / f"{agent_id}.png")


def _rename_in_dashboard(cfg, name: str) -> None:
    """The agent's own page carries its name in a few fixed places (see
    templates/agent_dashboard.html); agent-written parts are left alone."""
    f = agents.AGENTS_ROOT / cfg.folder / "dashboard.html"
    if not f.exists():
        return
    old = cfg.name
    text = f.read_text(encoding="utf-8")
    for before, after in (
        (f"window.OPENCLAVE_AGENT_NAME = '{old}'", f"window.OPENCLAVE_AGENT_NAME = '{name}'"),
        (f"<title>{old}</title>", f"<title>{name}</title>"),
        (f"<h1>{old}</h1>", f"<h1>{name}</h1>"),
        (f"Say something to {old}.", f"Say something to {name}."),
        (f"Message {old}…", f"Message {name}…"),
    ):
        text = text.replace(before, after)
    f.write_text(text, encoding="utf-8")


# What a clone takes from the original (claves, 2026-11-04): who it is and how
# it is set up. Never its memory, plan, backlog, files or chats: those stay
# with the clave the original is in, so claves stay separate.
CLONE_FILES = ("mission.md", "persona.md", "avatar.svg", "avatar.png")
CLONE_DIRS = ("skills",)


def clone_agent(agent_id: str, clave_id: str) -> dict:
    """The same agent for another clave: same name, colour, avatar, tagline,
    persona, mission, model, tools and skills; its own empty memory and chats.
    The manager is never cloned (it is in every clave already)."""
    import claves
    cfg = agents.AGENTS[agent_id]
    if agent_id == agents.manager_id():
        raise ValueError("The manager is in every clave already; it isn't cloned.")
    clave = claves.get(clave_id)
    if claves.name_taken_in_clave(cfg.name, clave_id):
        raise ValueError(f"{clave.name} already has an agent called {cfg.name}.")
    base = re.sub(r"[^a-z0-9-]", "", f"{agent_id}-{clave_id}")[:40]
    new_id, n = base, 2
    while new_id in agents.AGENTS or new_id in RESERVED_IDS:
        new_id, n = f"{base}-{n}", n + 1
    folder_name = f"{cfg.name} ({clave.name})"
    n = 2
    while (agents.AGENTS_ROOT / folder_name).exists():
        folder_name, n = f"{cfg.name} ({clave.name} {n})", n + 1
    source, target = agents.AGENTS_ROOT / cfg.folder, agents.AGENTS_ROOT / folder_name
    target.mkdir(parents=True)
    (target / "conversations").mkdir()
    (target / "memory").mkdir()
    for name in CLONE_FILES:
        if (source / name).is_file():
            shutil.copyfile(source / name, target / name)
    for name in CLONE_DIRS:
        if (source / name).is_dir():
            shutil.copytree(source / name, target / name)
    today = str(date.today())
    (target / "backlog.md").write_text(f"---\nUpdated: {today}\n---\n\n# {cfg.name} — Backlog\n\n", encoding="utf-8")
    (target / "plan.md").write_text(f"---\nStatus: active\nUpdated: {today}\n---\n\n# {cfg.name} — Current Plan\n\n"
                                    f"New in the {clave.name} clave (a clone of {cfg.name}). No plan yet.\n", encoding="utf-8")
    (target / "dashboard.html").write_text(render_dashboard(cfg.name, new_id, cfg.color, _initials(cfg.name)), encoding="utf-8")
    agent_file = source / agents.AGENT_FILE
    data = json.loads(agent_file.read_text(encoding="utf-8")) if agent_file.exists() else {}
    roles = [r for r in data.get("roles", list(cfg.roles)) if r not in (agents.MANAGER, agents.LEGACY_MANAGER)]
    for key in ("onboarding", "former_names", "duplicated_from"):
        data.pop(key, None)
    data.update(id=new_id, name=cfg.name, color=cfg.color, tagline=cfg.tagline, roles=roles,
                created=today, cloned_from=agent_id)
    save_json(target / agents.AGENT_FILE, data, ensure_ascii=False)
    agents.register(agents._agent_config(data, folder_name))
    try:
        llm_models.set_selected_model(new_id, llm_models.selected_model_id(agent_id))
    except ValueError:
        pass
    chosen = agent_tools.selected_optional(agent_id)
    if chosen is not None:
        agent_tools.set_optional(new_id, chosen)
    for suffix in (".png", ".base.png"):
        avatar = AVATAR_DIR / f"{agent_id}{suffix}"
        if avatar.exists():
            shutil.copyfile(avatar, AVATAR_DIR / f"{new_id}{suffix}")
    claves.place(new_id, clave_id)
    return {"id": new_id, "name": cfg.name, "folder": folder_name}
