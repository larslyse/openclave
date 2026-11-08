"""The manager's weekly audit (2026-11-03).

Runs right after the weekly cleanup (cleanup.run_weekly). The cleanup's
rules are plain file checks; the audit is the manager itself, run once in its
"Audit" conversation with a list of what changed since the last audit:

  - files the agents wrote or edited in their own folders (not chats)
  - recaps of conversations concluded and archived since then
  - proposals the owner declined lately, so they don't come back

It looks through them with its own read/search tools and files proposals
(propose_to_owner, proposals.py): things worth keeping in the main vault,
nudges for other agents, anything else it would do. Nothing changes until
the owner answers each one on the manager's page. Its reply in the Audit
conversation is the short summary of what it proposes, and arrives with a
push like any reply.

Gathering the list is deterministic; only the manager's run uses a model.

Control Panel → Weekly review (service_settings.WEEKLY_REVIEW) picks what it
looks for. "Suggest removing old models" and "Suggest adding new models" are
filed here directly (model_proposals), from llm_models.old_reason and what
model_updater found, so the ids are exact; the manager doesn't write them.
"""
import json
import time
from datetime import datetime
from pathlib import Path

import agents
import proposals
import recaps
import vault
from agents import AGENTS
from config import CONFIG

STATE_FILE = CONFIG.state_path("audit_state.json")
FIRST_LOOKBACK_DAYS = 30
MAX_FILES_PER_AGENT = 25
MAX_FILES = 120
# Folder parts and files that are chats, runtime state or the agent's own
# identity: not material for the main vault.
SKIP_PARTS = {"conversations", "requests", "skills", ".git", "__pycache__"}
SKIP_NAMES = {"agent.json", "page.json", "reminders.json", "response_times.json", "avatar.svg",
              "dashboard.html", "dashboard.html.bak", "persona.md", "inbox.md"}
TEXT_SUFFIXES = {".md", ".txt", ".csv", ".json"}


def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def changed_files(agent_id: str, since_ts: float) -> list[dict]:
    """Text files in the agent's folder modified after since_ts, newest first."""
    root = vault.agent_root(agent_id)
    out = []
    for f in root.rglob("*") if root.exists() else ():
        try:
            rel = f.relative_to(root)
            if not f.is_file() or f.suffix.lower() not in TEXT_SUFFIXES or f.name in SKIP_NAMES \
                    or any(p in SKIP_PARTS or p.startswith(".") for p in rel.parts):
                continue
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if mtime > since_ts:
            out.append({"path": str(f.relative_to(vault.VAULT_ROOT)), "size": f.stat().st_size,
                        "changed": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d"), "_m": mtime})
    out.sort(key=lambda x: x["_m"], reverse=True)
    return [{k: v for k, v in x.items() if k != "_m"} for x in out[:MAX_FILES_PER_AGENT]]


def gather(since_ts: float) -> dict:
    manager = agents.manager_id()
    files, concluded = {}, {}
    total = 0
    for agent_id in AGENTS:
        if agent_id == manager:
            continue
        found = changed_files(agent_id, since_ts)[:max(0, MAX_FILES - total)]
        total += len(found)
        if found:
            files[agent_id] = found
        done = recaps.concluded_since(agent_id, since_ts)
        if done:
            concluded[agent_id] = done
    return {"files": files, "concluded": concluded, "declined": proposals.recent_answered()}


def model_proposals() -> list[dict]:
    """File the model proposals the Weekly review asks for. Returns what was filed."""
    import llm_models
    import model_updater
    import service_settings
    review = service_settings.load()["weekly_review"]
    state = model_updater.load()
    answered = {i["title"].lower() for i in proposals.recent_answered()}
    filed = []

    def file(title, why, content, action, model_id):
        if title.lower() in answered:
            return  # declined lately: not again so soon
        try:
            filed.append(proposals.add("model", title, why, content, model_action=action, model_id=model_id))
        except ValueError:
            pass  # already waiting, or too many waiting

    if review["remove_old_models"]:
        keep = llm_models.in_use() | set(state["pinned"])
        shown = [m for m in llm_models.CATALOG if m.id not in llm_models.HIDDEN]
        for m in shown:
            if m.id in keep:
                continue
            reason = ("the provider no longer offers it" if m.id in state["gone"]
                      else llm_models.old_reason(m, shown))
            if reason:
                file(f"Remove old model: {m.display_name} ({m.route})", reason,
                     f"Take **{m.display_name}** ({llm_models.vendor(m)}, {m.route}) out of the model pickers: "
                     f"{reason}. No agent uses it. Restore in the Control Panel brings it back.", "hide", m.id)
    if review["add_new_models"]:
        for model_id, found in state["available"].items():
            m = found["model"]
            route = "API" if m["provider"] in ("xai", "anthropic") else "CLI"
            file(f"Add new model: {m['display_name']} ({route})", found.get("detail", ""),
                 f"Add **{m['display_name']}** ({llm_models.VENDORS.get(m['provider'], m['provider'])}, {route}) "
                 f"to the model pickers. It answered a test reply: {found.get('detail', '')}.", "add", model_id)
    return filed


def brief(material: dict, since: str, cleanup_line: str = "", review: dict | None = None,
          models_filed: list[dict] | None = None) -> str:
    """The audit instructions and material for the manager (model-only context)."""
    owner = CONFIG.owner_name or "the user"
    confidential = ", ".join(CONFIG.confidential_folders) or "none configured"
    review = review or {}
    looks = []
    if review.get("vault_notes", True):
        looks.append(
            f"**Worth keeping in the main vault** (kind=vault_note): settled facts, decisions, specs, how-tos, "
            f"shopping or project outcomes that {owner} would look for in their own notes, not only inside an agent. "
            "Search the vault first (search_vault) and prefer appending to the existing note that covers it; create a "
            "new note only when none does, in an existing folder, following the vault's note conventions you see "
            "in nearby notes. Write the content distilled and ready to paste: short, in their notes' style, with a "
            "source line naming the agent file it came from. Skip anything already in the vault, speculation, "
            f"chit-chat, and anything private to one agent's job. Never propose writing in agent folders or in "
            f"confidential folders ({confidential}).")
    if review.get("nudge_agents", True):
        looks.append("**Nudges** (kind=message_agent): an agent that's stuck, stale or doing something wrong, where a "
                     "message from you would help. Put the whole message in content.")
    if review.get("tasks", True):
        looks.append("**Anything else you'd do** (kind=task): a cleanup, a fix, a backlog item, a skill to write. Say "
                     "exactly what you'd do in content.")
    if review.get("merge_agents", True):
        roster = "; ".join(f"{a.name} ({a.tagline})" if a.tagline else a.name
                           for a in AGENTS.values() if a.id != agents.manager_id())
        looks.append(
            f"**Agents to merge** (kind=task): two agents whose domains overlap so much that {owner} can't tell who "
            f"handles what. Roughly 10 agents is plenty; fewer is easier to keep track of. Only propose a merge you'd "
            "defend from their missions (read them), say which one folds into which and why, and put this in content "
            "for the owner to run on the server (it backs everything up first): "
            "`python3 scripts/merge_agent.py <source id> <target id> --folder`. Current agents: " + roster)
    lines = [
        f"# Weekly audit: what changed since {since}",
        "",
        f"You are the manager. Look through what the other agents wrote since the last audit and propose what {owner} "
        "would want. You only propose: file each one with propose_to_owner, and don't change anything outside your "
        "own folder and don't message other agents during this audit; propose that instead.",
        "",
        f"What to look for ({owner} picked these in the Control Panel's Weekly review; propose nothing else):",
        *[f"{n}. {text}" for n, text in enumerate(looks, 1)],
        "",
        "Fewer, better proposals: at most about 8, only ones you'd defend. Nothing worth proposing is a fine result.",
        "When done, reply here with a short summary: one line per proposal (what and why), so "
        f"{owner} can answer from their phone. No preamble.",
    ]
    if cleanup_line:
        lines += ["", f"This week's cleanup (separate, already on your page): {cleanup_line}"]
    if models_filed:
        lines += ["", "Model proposals already filed for this week (by the service, not you; mention them in "
                  "your summary in one line): " + "; ".join(p["title"] for p in models_filed)]
    lines += ["", "## Files changed in agent folders"]
    if material["files"]:
        for agent_id, found in material["files"].items():
            lines.append(f"- **{AGENTS[agent_id].name}**")
            lines += [f"  - {f['path']} ({f['changed']}, {f['size']} bytes)" for f in found]
    else:
        lines.append("- none")
    lines += ["", "## Conversations concluded and archived (recaps)"]
    if material["concluded"]:
        for agent_id, cards in material["concluded"].items():
            for c in cards:
                lines.append(f"- **{AGENTS[agent_id].name}** · {c['title']}: {c['about']}")
    else:
        lines.append("- none")
    if material["declined"]:
        lines += ["", f"## Answered lately (don't propose these again unless something changed)"]
        lines += [f"- {i['status']}: {i['title']}" + (f" ({i['note']})" if i.get("note") else "")
                  for i in material["declined"][-20:]]
    return "\n".join(lines)


def run(cleanup_line: str = "", now: float | None = None) -> dict:
    """Queue the manager's audit run. Returns what was sent (for tests/logs)."""
    import chat_jobs
    manager = agents.manager_id()
    if not manager:
        return {"queued": False, "reason": "no manager"}
    now = now or time.time()
    import service_settings
    review = service_settings.load()["weekly_review"]
    models_filed = model_proposals()
    if not any(review[k] for k in ("vault_notes", "nudge_agents", "tasks")):
        # Nothing for the manager to look for; the model proposals stand alone.
        return {"queued": False, "reason": "nothing ticked for the manager in Weekly review",
                "models": len(models_filed)}
    state = _load()
    since_ts = float(state.get("last_ts") or now - FIRST_LOOKBACK_DAYS * 86_400)
    since = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d")
    material = gather(since_ts)
    vault.ensure_conversation(manager, proposals.AUDIT_CONVERSATION, "Audit")
    request_id = f"audit-{datetime.fromtimestamp(now):%Y%m%d%H%M%S}"
    chat_jobs.submit(manager, request_id, proposals.AUDIT_CONVERSATION,
                     f"Weekly audit: what changed since {since}. Propose what's worth doing.",
                     extra={"context_note": brief(material, since, cleanup_line, review, models_filed)}, schedule=False)
    chat_jobs.kick(manager)
    state["last_ts"] = now
    _save(state)
    return {"queued": True, "request_id": request_id,
            "files": sum(len(v) for v in material["files"].values()),
            "concluded": sum(len(v) for v in material["concluded"].values()), "models": len(models_filed)}
