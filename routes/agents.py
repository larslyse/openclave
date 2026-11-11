"""Agents: the list, creating one, its profile, name, colour, identity files and the tab order."""
from fastapi import APIRouter, HTTPException
import agent_factory
import agent_order
import vault_access
import llm_models
import connections
import vault
import claves
from agents import AGENTS
from schemas import (
    AgentColorRequest, AgentSummary, CreateAgentRequest, IdentityBodyRequest, RenameAgentRequest,
    SetAgentOrderRequest,
)
from routes.common import _join_clave, _register_agent_routes
from routes.library import _avatar_version

router = APIRouter()


@router.get("/api/agents", response_model=list[AgentSummary])
def api_agents():
    out = []
    clave_names = {c.id: c.name for c in claves.list_claves()} if claves.enabled() else {}
    for agent_id in agent_order.get_order():
        cfg = AGENTS[agent_id]
        plan = vault.read_plan(agent_id)
        backlog = vault.read_backlog(agent_id)
        model = llm_models.get(llm_models.selected_model_id(agent_id))
        out.append(AgentSummary(
            id=agent_id,
            name=cfg.name,
            color=cfg.color,
            tagline=cfg.tagline,
            roles=list(cfg.roles),
            domains=vault.agent_domains(agent_id),
            conversation_count=len(vault.list_conversations(agent_id)),
            plan_status=plan["status"],
            backlog_open=sum(1 for i in backlog["items"] if not i["done"]),
            backlog_total=len(backlog["items"]),
            model_id=model.id,
            model_name=model.display_name,
            model_supports=connections.supported_by_model(agent_id),
            avatar_v=_avatar_version(agent_id),
            **vault.latest_activity(agent_id),
            clave=clave_names.get(claves.clave_of(agent_id)) if clave_names else None,
        ))
    return out


@router.post("/api/agents")
def api_create_agent(req: CreateAgentRequest):
    try:
        created = agent_factory.create_agent(req.name, req.purpose, req.model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _register_agent_routes(created["id"])
    _join_clave(created["id"], req.clave)
    return created


@router.get("/api/agents/{agent_id}/profile")
def api_agent_profile(agent_id: str):
    """Everything the Control Panel's Edit panel shows."""
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    cfg = AGENTS[agent_id]
    return {"id": agent_id, "name": cfg.name, "tagline": cfg.tagline, "color": cfg.color,
            "mission": vault.read_identity_body(agent_id, "mission"),
            "persona": vault.read_identity_body(agent_id, "persona"),
            "vault_write": vault_access.folders(agent_id)}


@router.post("/api/agents/{agent_id}/vault-write")
def api_agent_vault_write(agent_id: str, req: dict):
    """Control Panel → Edit: vault folders this agent may write in (vault_access.py)."""
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    wanted = req.get("folders")
    if not isinstance(wanted, list) or not all(isinstance(f, str) for f in wanted):
        raise HTTPException(status_code=400, detail="folders must be a list of vault folder paths")
    try:
        return {"folders": vault_access.set_folders(agent_id, wanted)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/agents/{agent_id}/name")
def api_rename_agent(agent_id: str, req: RenameAgentRequest):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        return agent_factory.rename_agent(agent_id, req.name, req.tagline)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/agents/{agent_id}/color")
def api_agent_color(agent_id: str, req: AgentColorRequest):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        return agent_factory.set_color(agent_id, req.color)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/api/agents/{agent_id}/identity/{kind}")
def api_set_identity(agent_id: str, kind: str, req: IdentityBodyRequest):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    if kind not in ("mission", "persona"):
        raise HTTPException(status_code=404, detail="Unknown identity file")
    try:
        vault.set_identity_body_by_owner(agent_id, kind, req.body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return vault.read_identity_body(agent_id, kind)


@router.post("/api/agents/order")
def api_set_agent_order(req: SetAgentOrderRequest):
    try:
        agent_order.set_order(req.order)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"order": req.order}
