"""Conversations across agents: recaps, moving chats and files between agents, handover, recent replies."""
from fastapi import APIRouter, HTTPException
import vault
import chat_jobs
import recaps
import chat_service
import handover
from agents import AGENTS
from schemas import MoveWorkspaceFileRequest, HandoverRequest, MoveConversationRequest
from routes.common import _skill_agent

router = APIRouter()


# ── Recaps (recaps.py): archived conversations as short cards ──
@router.get("/api/agents/{agent_id}/recaps")
def api_agent_recaps(agent_id: str):
    _skill_agent(agent_id)
    return {"recaps": recaps.list_for(agent_id), "can_write": recaps.available()}


@router.get("/api/agents/{agent_id}/recaps/{conversation_id}")
def api_agent_recap_chat(agent_id: str, conversation_id: str):
    _skill_agent(agent_id)
    try:
        convo = vault.get_conversation(agent_id, conversation_id, archived=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if convo is None:
        raise HTTPException(status_code=404, detail="no such archived conversation")
    return {"title": convo["title"], "date": convo["date"], "summary": convo["summary"],
            "turns": [{"role": t["role"], "content": t["content"], "ts": t["ts"]} for t in convo["turns"]]}


@router.post("/api/agents/{agent_id}/recaps/{conversation_id}/write")
def api_agent_recap_write(agent_id: str, conversation_id: str):
    _skill_agent(agent_id)
    if not recaps.available():
        raise HTTPException(status_code=503, detail="Recaps need an Anthropic API key")
    try:
        recap = recaps.make_now(agent_id, conversation_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 -- provider errors
        raise HTTPException(status_code=502, detail=f"Could not write the recap: {exc}")
    if not recap:
        raise HTTPException(status_code=502, detail="The model gave no usable recap; try again")
    return recap


@router.post("/api/agents/{agent_id}/recaps/{conversation_id}/restore")
def api_agent_recap_restore(agent_id: str, conversation_id: str):
    _skill_agent(agent_id)
    try:
        if not vault.unarchive_conversation(agent_id, conversation_id):
            raise HTTPException(status_code=404, detail="no such archived conversation")
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"restored": conversation_id}


@router.post("/api/files/move")
def move_workspace_file(req: MoveWorkspaceFileRequest):
    # Global, not agent-scoped: reached only through the trusted shell's
    # confirm-then-forward bridge, same as /api/conversations/move.
    if req.source_agent not in AGENTS or req.target_agent not in AGENTS:
        raise HTTPException(status_code=400, detail="unknown agent")
    try:
        return vault.move_workspace_file(req.source_agent, req.target_agent, req.path)
    except (ValueError, vault.WriteScopeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/conversations/move")
def move_conversation(req: MoveConversationRequest):
    if req.source_agent not in AGENTS or req.target_agent not in AGENTS:
        raise HTTPException(status_code=400, detail="unknown agent")
    try:
        moved = chat_jobs.move_conversation(req.source_agent, req.target_agent, req.conversation_id)
    except (chat_service.AgentBusyError, chat_jobs.AgentBusyError, FileExistsError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not moved:
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"id": req.conversation_id, "source_agent": req.source_agent, "target_agent": req.target_agent}


@router.post("/api/conversations/handover")
def handover_conversation(req: HandoverRequest):
    # Global like /api/conversations/move: the desktop page reaches it only
    # through the trusted shell's confirm-then-forward bridge (dashboard_shell.js).
    if req.source_agent not in AGENTS or req.target_agent not in AGENTS:
        raise HTTPException(status_code=400, detail="unknown agent")
    try:
        result = handover.handover(req.source_agent, req.target_agent, req.conversation_id, req.files)
    except (chat_service.AgentBusyError, chat_jobs.AgentBusyError, FileExistsError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (ValueError, vault.WriteScopeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return result


@router.get("/api/replies")
def api_recent_replies(after: int = 0):
    """Replies finished after `after` (ms), oldest first: Autojump polls this."""
    return chat_jobs.recent_replies(after)
