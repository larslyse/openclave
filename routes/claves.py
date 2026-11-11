"""Group chat: the room, channels, claves and messages between agents."""
from fastapi import APIRouter, HTTPException
import agent_factory
import agent_order
import channels
import service_settings
import agent_messages
import room
import claves
import agents as agents_module
from agents import AGENTS
from schemas import (
    RoomPost, CreateClaveRequest, UpdateClaveRequest, PlaceAgentRequest, RemoveFromClaveRequest,
    ClavesEnabledRequest, AgentClaveRequest, CloneAgentRequest,
)
from routes.common import _clave_call, _register_agent_routes

router = APIRouter()


@router.get("/api/room")
def api_room(after: int = 0, clave: str = claves.EVERYONE):
    try:
        return room.messages(after, clave=clave)
    except KeyError:
        raise HTTPException(status_code=404, detail="Room not found")


@router.post("/api/room")
def api_room_post(req: RoomPost):
    try:
        message = room.post(req.text, req.clave)
    except KeyError:
        raise HTTPException(status_code=404, detail="Clave not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    message["name"] = room._speaker(message)
    return message


# ── Channels (channels.py): group chats the owner makes ──
def _channel_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError:
        raise HTTPException(status_code=404, detail="Channel not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/channels")
def api_channels():
    return channels.list_channels()


@router.post("/api/channels")
def api_channel_create(req: dict):
    return _channel_call(channels.create, str(req.get("name", "")), req.get("members") or [])


@router.patch("/api/channels/{channel_id}")
def api_channel_update(channel_id: str, req: dict):
    name = req.get("name")
    return _channel_call(channels.update, channel_id, str(name) if name is not None else None, req.get("members"))


@router.delete("/api/channels/{channel_id}")
def api_channel_delete(channel_id: str):
    _channel_call(channels.delete, channel_id, room._file(channel_id))
    return {"deleted": channel_id}


# ── Claves (claves.py): groups of agents, each with its own group chat ──
@router.get("/api/claves")
def api_claves():
    return claves.overview()


@router.post("/api/claves/enabled")
def api_claves_enabled(req: ClavesEnabledRequest):
    service_settings.update({"multiple_claves": req.enabled})
    if req.enabled:
        claves.ensure_default(agent_order.get_order())
    return claves.overview()


@router.post("/api/claves")
def api_create_clave(req: CreateClaveRequest):
    _clave_call(claves.create, req.name, req.members)
    return claves.overview()


@router.patch("/api/claves/{clave_id}")
def api_update_clave(clave_id: str, req: UpdateClaveRequest):
    kwargs = {"name": req.name, "members": req.members}
    if req.set_lead:
        kwargs["lead"] = req.lead or None
    _clave_call(claves.update, clave_id, **kwargs)
    return claves.overview()


@router.delete("/api/claves/{clave_id}")
def api_archive_clave(clave_id: str):
    _clave_call(claves.archive, clave_id)
    return claves.overview()


@router.post("/api/claves/active")
def api_active_clave(req: dict):
    _clave_call(claves.set_active, str(req.get("clave") or ""))
    return claves.overview()


@router.post("/api/claves/place")
def api_place_agent(req: PlaceAgentRequest):
    _clave_call(claves.place, req.agent, req.to, index=req.index, lead=req.lead)
    return claves.overview()


@router.post("/api/claves/clone")
def api_clone_agent(req: CloneAgentRequest):
    """The same agent for another clave, with its own empty memory and chats."""
    if req.agent not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    created = _clave_call(agent_factory.clone_agent, req.agent, req.to)
    _register_agent_routes(created["id"])
    return {**created, **claves.overview()}


@router.post("/api/claves/remove")
def api_remove_from_clave(req: RemoveFromClaveRequest):
    _clave_call(claves.remove, req.agent, req.clave)
    return claves.overview()


@router.get("/api/agents/{agent_id}/claves")
def api_agent_claves(agent_id: str):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"enabled": claves.enabled(), "selected": claves.clave_of(agent_id),
            "manager": agent_id == agents_module.manager_id(),
            "claves": [{"id": c.id, "name": c.name} for c in claves.list_claves()]}


@router.post("/api/agents/{agent_id}/claves")
def api_set_agent_clave(agent_id: str, req: AgentClaveRequest):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"selected": _clave_call(claves.set_agent_clave, agent_id, req.clave or None)}


@router.get("/api/agent-messages")
def api_agent_messages():
    return agent_messages.threads()
