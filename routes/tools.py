"""Tools, skills, scheduled tasks and reminders."""
from fastapi import APIRouter, HTTPException
import agent_order
import agent_tools
import llm_client
import reminders
import scheduled_tasks
import skills
import connections
import agents as agents_module
from agents import AGENTS
from schemas import SetAgentToolsRequest
from routes.common import _skill_agent

router = APIRouter()


# ── Reminders (agents' scheduled messages) and Web Push ──
@router.get("/api/reminders")
def api_reminders():
    """Control Panel overview: every agent's upcoming reminders and recent sends."""
    out = []
    for agent_id in agent_order.get_order():
        upcoming = reminders.list_for(agent_id)
        sent = reminders.history_for(agent_id)[:10]
        if upcoming or sent:
            out.append({"agent_id": agent_id, "agent_name": AGENTS[agent_id].name, "upcoming": upcoming, "sent": sent})
    return {"now": reminders.describe_now(), "agents": out}


@router.delete("/api/reminders/{agent_id}/{reminder_id}")
def api_cancel_reminder(agent_id: str, reminder_id: str):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not reminders.cancel(agent_id, reminder_id):
        raise HTTPException(status_code=404, detail="Reminder not found")
    return {"cancelled": reminder_id}


# ── Scheduled tasks (scheduled_tasks.py): agents' recurring jobs ──
def _task_agent(agent_id: str) -> None:
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")


@router.get("/api/tasks")
def api_tasks():
    """Control Panel overview: every agent's scheduled tasks with their last run."""
    return scheduled_tasks.overview()


@router.post("/api/tasks/{agent_id}")
def api_add_task(agent_id: str, req: dict):
    _task_agent(agent_id)
    try:
        return scheduled_tasks.add(agent_id, req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/tasks/{agent_id}/{task_id}")
def api_update_task(agent_id: str, task_id: str, req: dict):
    _task_agent(agent_id)
    try:
        task = scheduled_tasks.update(agent_id, task_id, req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.delete("/api/tasks/{agent_id}/{task_id}")
def api_delete_task(agent_id: str, task_id: str):
    _task_agent(agent_id)
    if not scheduled_tasks.delete(agent_id, task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    return {"deleted": task_id}


@router.post("/api/tasks/{agent_id}/{task_id}/run")
def api_run_task(agent_id: str, task_id: str):
    """Run now, in the background; the result lands in the task's conversation."""
    _task_agent(agent_id)
    task = next((t for t in scheduled_tasks._load(agent_id)["tasks"] if t["id"] == task_id), None)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if scheduled_tasks.get(agent_id, task_id)["running"]:
        raise HTTPException(status_code=409, detail="This task is already running")
    scheduled_tasks.run_in_background(agent_id, task, manual=True)
    return {"started": task_id, "conversation_id": scheduled_tasks.conversation_id(task_id)}


# ── Skills (skills.py): know-how agents load on demand ──
@router.get("/api/agents/{agent_id}/skills")
def api_agent_skills(agent_id: str):
    _skill_agent(agent_id)
    return {"skills": skills.list_for(agent_id)}


@router.get("/api/agents/{agent_id}/skills/{scope}/{name}")
def api_skill_text(agent_id: str, scope: str, name: str):
    _skill_agent(agent_id)
    try:
        return {"key": f"{scope}/{name}", "text": skills.text(agent_id, f"{scope}/{name}")}
    except (skills.SkillError, OSError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/api/agents/{agent_id}/skills/{scope}/{name}")
def api_skill_action(agent_id: str, scope: str, name: str, req: dict):
    """{"on": bool} switches it for this agent; {"approve": true} enables an import."""
    _skill_agent(agent_id)
    key = f"{scope}/{name}"
    try:
        if req.get("approve"):
            return skills.approve(agent_id, key)
        return skills.set_enabled(agent_id, key, bool(req.get("on")))
    except skills.SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/api/agents/{agent_id}/skills/{scope}/{name}")
def api_skill_delete(agent_id: str, scope: str, name: str):
    _skill_agent(agent_id)
    try:
        skills.delete(agent_id, f"{scope}/{name}")
    except skills.SkillError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"deleted": f"{scope}/{name}"}


@router.post("/api/skills/import")
def api_skill_import(req: dict):
    """{source | text, target: "shared" | agent id, name?, preview?, replace?}"""
    try:
        return skills.import_skill(str(req.get("source") or ""), pasted=req.get("text"),
                                   target=str(req.get("target") or "shared"), name=req.get("name") or None,
                                   preview=bool(req.get("preview")), replace=bool(req.get("replace")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"couldn't fetch it: {exc}")


@router.get("/api/tools")
def api_tools():
    """Tool catalog for the Control Panel's per-agent tool editor."""
    return {
        "core": list(agent_tools.CORE_TOOLS),
        "optional": [
            {"name": t["function"]["name"], "summary": t["function"]["description"].split(". ")[0].rstrip(".") + "."}
            for t in llm_client.TOOLS if t["function"]["name"] in agent_tools.OPTIONAL_TOOLS
        ] + [{"name": name, "summary": text.split(". ")[0] + "."} for name, text in agent_tools.CAPABILITIES.items()],
    }


@router.get("/api/agents/{agent_id}/tools")
def api_agent_tools(agent_id: str):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    chosen = agent_tools.selected_optional(agent_id)
    available = list(agent_tools.optional_for(agent_id))
    return {"available": available, "selected": available if chosen is None else chosen,
            "customized": chosen is not None, "onboarding": agents_module.is_onboarding(agent_id),
            "connections": connections.status(agent_id), "connection_hint": connections.route_hint(agent_id),
            "writes": f"Only its own folder (Agents/{AGENTS[agent_id].name}/)"}


@router.post("/api/agents/{agent_id}/tools")
def api_set_agent_tools(agent_id: str, req: SetAgentToolsRequest):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        return {"selected": agent_tools.set_optional(agent_id, req.tools)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
