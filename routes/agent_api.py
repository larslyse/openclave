"""Per-agent API under /<agent_id>/: chat, voice, chat requests, conversations,
folders, purge and recaps (the rest is in routes/agent_workspace.py).
build_agent_router makes one router per agent; main._register_agent_routes mounts it."""
import base64
import mimetypes
import os
import tempfile
from pathlib import Path
from fastapi import APIRouter, HTTPException, Response
import agent_order
import purge
import service_settings
import request_phase
import vault
import chat_jobs
import conversation_titles
import recaps
import folders
import claves
import chat_service
import handover
import voice_engine
from agents import AGENTS
from routes.agent_workspace import add_workspace_routes
from schemas import (
    AttachmentUploadResponse, ChatRequest, ChatRequestCreate, ChatRequestStatus, ChatResponse,
    ConversationDetail, ConversationSummary, RenameConversationRequest, SetAgentOrderRequest,
    FolderRequest, UploadAttachmentRequest, VoiceReplyRequest, VoiceTranscribeRequest,
    VoiceTranscribeResponse, VoiceTurnResponse,
)
from routes.common import VOICE_ENABLED_AGENTS, _clave_call
from routes.conversations import api_agent_recap_write


def build_agent_router(agent_id: str) -> APIRouter:
    router = APIRouter()

    @router.post("/chat", response_model=ChatResponse)
    def chat(req: ChatRequest):
        timings = {}
        conversation_id = req.conversation_id or vault.new_conversation_id()
        if not req.message.strip():
            raise HTTPException(status_code=400, detail="message cannot be empty")
        try:
            reply = chat_service.run_chat(agent_id, conversation_id, req.message, req.attachments, timings=timings)
        except (ValueError, vault.WriteScopeError, chat_service.UnsupportedAttachmentError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except chat_service.AgentBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except chat_service.ProviderReplyError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return ChatResponse(conversation_id=conversation_id, reply=reply, elapsed_ms=timings.get("elapsed_ms"))

    if agent_id in VOICE_ENABLED_AGENTS:
        @router.get("/voice/languages")
        def voice_languages():
            # The EN/xx button by the mic ([voice] second_language).
            return {"languages": chat_service.voice_languages()}

        def _check_language(language: str) -> None:
            if language not in chat_service.voice_languages():
                raise HTTPException(status_code=400, detail=f"voice language must be one of {', '.join(chat_service.voice_languages())}")

        @router.post("/voice/transcribe", response_model=VoiceTranscribeResponse)
        def voice_transcribe(req: VoiceTranscribeRequest):
            _check_language(req.language)
            # Split from the reply step (2026-09-21, the owner: wants to see what
            # he said without waiting on the full reply+speech round trip).
            # No agent_execution lock — stateless, doesn't touch conversation
            # history or call an LLM, so it can't collide with a reply call.
            ext = mimetypes.guess_extension(req.media_type) or ".webm"
            in_fd, audio_path = tempfile.mkstemp(suffix=ext, prefix="openclave-voice-in-")
            try:
                with os.fdopen(in_fd, "wb") as f:
                    f.write(base64.b64decode(req.audio_base64))
                try:
                    transcript = chat_service.transcribe_voice_audio(agent_id, audio_path, req.language)
                except chat_service.ProviderReplyError as exc:
                    raise HTTPException(status_code=502, detail=str(exc))
                except voice_engine.VoiceEngineError as exc:
                    raise HTTPException(status_code=502, detail=f"voice engine: {exc}")
            finally:
                Path(audio_path).unlink(missing_ok=True)
            return VoiceTranscribeResponse(transcript=transcript)

        @router.post("/voice/reply", response_model=VoiceTurnResponse)
        def voice_reply(req: VoiceReplyRequest):
            _check_language(req.language)
            if not req.transcript.strip():
                raise HTTPException(status_code=400, detail="transcript cannot be empty")
            conversation_id = req.conversation_id or vault.new_conversation_id()
            timings = {}
            try:
                with chat_service.agent_execution(agent_id):
                    result = chat_service.run_voice_reply_locked(agent_id, conversation_id, req.transcript, language=req.language, timings=timings)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except chat_service.AgentBusyError as exc:
                raise HTTPException(status_code=409, detail=str(exc))
            except chat_service.ProviderReplyError as exc:
                raise HTTPException(status_code=502, detail=str(exc))
            # reply_audio_path is None when synthesis failed but the reply
            # text itself is good (run_voice_reply_locked treats a TTS
            # failure as non-fatal) — text still reaches the client.
            audio_base64 = None
            if result["reply_audio_path"]:
                try:
                    audio_base64 = base64.b64encode(Path(result["reply_audio_path"]).read_bytes()).decode()
                finally:
                    Path(result["reply_audio_path"]).unlink(missing_ok=True)
            return VoiceTurnResponse(
                conversation_id=result["conversation_id"], transcript=result["transcript"], reply=result["reply"],
                audio_base64=audio_base64, audio_mime=result["audio_mime"], elapsed_ms=timings.get("elapsed_ms"),
                model_name=chat_service.reply_model_label(chat_service.VOICE_MODEL_ID, voice=True),
            )

    @router.get("/phase")
    def get_phase():
        # Coarse, read-only, no side effects — polled by both frontends'
        # "Working for Xs" indicator while a request (sync or async) is in
        # flight, purely cosmetic if never polled. "text": the reply so far
        # while an API route streams it (empty on the CLI routes).
        return {"phase": request_phase.get_phase(agent_id), "text": request_phase.get_text(agent_id)}

    @router.post("/chat-requests", response_model=ChatRequestStatus, status_code=202)
    def create_chat_request(req: ChatRequestCreate):
        try:
            result, created = chat_jobs.submit(
                agent_id, req.request_id, req.conversation_id, req.message, req.attachments
            )
        except (ValueError, vault.WriteScopeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except (chat_jobs.RequestConflictError, chat_jobs.AgentBusyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return result

    @router.post("/attachments", response_model=AttachmentUploadResponse)
    def upload_attachment(req: UploadAttachmentRequest):
        conversation_id = req.conversation_id or vault.new_conversation_id()
        try:
            data = base64.b64decode(req.data_base64, validate=True)
        except Exception:
            raise HTTPException(status_code=400, detail="data_base64 is not valid base64")
        try:
            meta = vault.save_attachment(agent_id, conversation_id, req.filename, req.media_type, data)
        except (ValueError, vault.WriteScopeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return AttachmentUploadResponse(conversation_id=conversation_id, attachment=meta)

    @router.get("/conversations/{conversation_id}/attachments/{attachment_id}")
    def get_attachment(conversation_id: str, attachment_id: str):
        try:
            result = vault.read_attachment(agent_id, conversation_id, attachment_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if result is None:
            raise HTTPException(status_code=404, detail="attachment not found")
        data, media_type = result
        return Response(content=data, media_type=media_type)

    @router.get("/chat-requests/{request_id}", response_model=ChatRequestStatus)
    def get_chat_request(request_id: str, watching: bool = False):
        try:
            result = chat_jobs.get(agent_id, request_id, watching=watching)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if result is None:
            raise HTTPException(status_code=404, detail="chat request not found")
        return result

    @router.post("/chat-requests/{request_id}/cancel")
    def cancel_chat_request(request_id: str):
        try:
            result = chat_jobs.cancel(agent_id, request_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if result is None:
            raise HTTPException(status_code=404, detail="chat request not found")
        return result

    @router.get("/agent-tabs")
    def agent_tabs():
        # Every agent for the desktop tab strip, this one included (marked
        # current, so the tabs don't reshuffle when switching), in dashboard
        # order. Agent-scoped because the sandboxed page can't reach /api/agents.
        # With claves on, only the active clave's agents (claves.active), plus this one.
        shown = set(claves.visible(agent_order.get_order())) | {agent_id}
        order = [aid for aid in agent_order.get_order() if aid in shown]
        recent = service_settings.load()["sort_agent_tabs"] == "recent"
        if recent:  # Control Panel → Sorting: latest message first (stable for ties)
            order.sort(key=lambda aid: -(vault.latest_activity(aid)["last_activity_ms"] or 0))
        return [{"id": aid, "name": AGENTS[aid].name, "color": AGENTS[aid].color, "current": aid == agent_id,
                 "sorted": "recent" if recent else "manual"} for aid in order]

    @router.get("/claves")
    def agent_page_claves():
        # The clave picker on the agent page (left of the bell); agent-scoped
        # like agent-tabs because the sandboxed page can't reach /api.
        data = claves.overview()
        return {"enabled": data["enabled"], "active": data["active"],
                "claves": [{"id": c["id"], "name": c["name"]} for c in data["claves"]]}

    @router.post("/claves/active")
    def agent_page_set_clave(req: dict):
        _clave_call(claves.set_active, str(req.get("clave") or ""))
        return agent_page_claves()

    @router.post("/agent-tabs/order")
    def set_agent_tabs_order(req: SetAgentOrderRequest):
        # Dragging a tab saves the same order as the dashboard tiles.
        try:
            agent_order.set_order(req.order)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"order": req.order}

    @router.get("/move-targets")
    def move_targets():
        # Every agent, in every clave: moving and handing over work across claves.
        names = {c.id: c.name for c in claves.list_claves()} if claves.enabled() else {}
        out = []
        for a in AGENTS.values():
            if a.id == agent_id:
                continue
            clave = names.get(claves.clave_of(a.id)) if names else None
            out.append({"id": a.id, "name": a.name, **({"clave": clave} if clave else {})})
        return out

    @router.get("/conversations", response_model=list[ConversationSummary])
    def list_conversations():
        return sort_conversations(vault.list_conversations(agent_id))

    @router.get("/conversation-search")
    def search_conversations(q: str = ""):
        return vault.search_conversations(agent_id, q[:200])

    @router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
    def get_conversation(conversation_id: str):
        convo = vault.get_conversation(agent_id, conversation_id)
        if convo is None:
            # An archived chat opened from Recaps: shown with its recap, and
            # the next message brings it back (vault.restore_if_archived).
            convo = vault.get_conversation(agent_id, conversation_id, archived=True)
            if convo is not None:
                convo["archived"] = True
                convo["recap"] = recaps.card(agent_id, conversation_id)
        if convo is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        for turn in convo["turns"]:
            turn["model_name"] = chat_service.reply_model_label(turn.get("model_id"), voice=turn.get("voice"))
            sender = turn.get("from_agent")
            if sender:
                turn["from_name"] = AGENTS[sender].name if sender in AGENTS else sender
        return convo

    @router.get("/conversations/{conversation_id}/handover-files")
    def handover_files(conversation_id: str):
        # Agent-scoped so the sandboxed desktop page can show the handover dialog.
        try:
            files = handover.related_files(agent_id, conversation_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if files is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return files

    @router.delete("/conversations/{conversation_id}")
    def delete_conversation(conversation_id: str):
        try:
            deleted = chat_jobs.delete_conversation(agent_id, conversation_id)
        except (chat_service.AgentBusyError, chat_jobs.AgentBusyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if not deleted:
            raise HTTPException(status_code=404, detail="conversation not found")
        return {"deleted": conversation_id}

    @router.patch("/conversations/{conversation_id}")
    def rename_conversation(conversation_id: str, req: RenameConversationRequest):
        # Rename, and/or move into a folder (folders.py; "" takes it out).
        if req.title is None and req.folder is None:
            raise HTTPException(status_code=400, detail="nothing to change")
        out = {"id": conversation_id}
        if req.title is not None:
            if not req.title.strip():
                raise HTTPException(status_code=400, detail="title cannot be empty")
            if not vault.rename_conversation(agent_id, conversation_id, req.title):
                raise HTTPException(status_code=404, detail="conversation not found")
            out["title"] = req.title.strip()
        if req.folder is not None:
            try:
                done = folders.set_conversation_folder(agent_id, conversation_id, req.folder or None)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            if not done:
                raise HTTPException(status_code=404, detail="conversation not found")
            out["folder"] = req.folder.strip() or None
        return out

    # Chat folders (folders.py): plain buckets the owner drags chats into.
    @router.get("/folders")
    def list_folders():
        return folders.list_folders(agent_id)

    @router.post("/folders")
    def create_folder(req: FolderRequest):
        try:
            return folders.create_folder(agent_id, req.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @router.patch("/folders/{name}")
    def rename_folder(name: str, req: FolderRequest):
        try:
            return folders.rename_folder(agent_id, name, req.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @router.post("/folders/{name}/conversations")
    def new_folder_conversation(name: str):
        try:
            return {"id": folders.new_conversation(agent_id, name), "folder": name}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.delete("/folders/{name}")
    def delete_folder(name: str):
        folders.delete_folder(agent_id, name)
        return {"deleted": name}

    # Purge (purge.py): preview first, then purge, with undo.
    def _purge_call(fn):
        try:
            return fn()
        except (chat_service.AgentBusyError, chat_jobs.AgentBusyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="conversation not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/conversations/{conversation_id}/purge/preview")
    def purge_preview(conversation_id: str):
        return _purge_call(lambda: purge.preview(agent_id, conversation_id))

    @router.post("/conversations/{conversation_id}/purge")
    def purge_apply(conversation_id: str, req: dict):
        summary = str(req.get("summary") or "")
        return _purge_call(lambda: chat_jobs.while_idle(agent_id, conversation_id,
                                                        lambda: purge.apply(agent_id, conversation_id, summary), "purging it"))

    @router.post("/conversations/{conversation_id}/purge/undo")
    def purge_undo(conversation_id: str):
        return _purge_call(lambda: chat_jobs.while_idle(agent_id, conversation_id,
                                                        lambda: purge.undo(agent_id, conversation_id), "restoring it"))

    @router.post("/conversations/{conversation_id}/archive")
    def archive_conversation(conversation_id: str):
        # Out of the list and into the Recaps tab (recaps.py writes its card).
        try:
            done = chat_jobs.archive_conversation(agent_id, conversation_id)
        except (chat_service.AgentBusyError, chat_jobs.AgentBusyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except (ValueError, FileExistsError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not done:
            raise HTTPException(status_code=404, detail="conversation not found")
        return {"archived": conversation_id}

    # Recaps for this agent's own pages (the sandboxed desktop frame only
    # reaches agent-scoped routes).
    @router.get("/recaps")
    def agent_recaps():
        return {"recaps": recaps.list_for(agent_id), "can_write": recaps.available()}

    @router.post("/conversations/{conversation_id}/recap")
    def write_recap(conversation_id: str):
        return api_agent_recap_write(agent_id, conversation_id)

    @router.post("/conversations/{conversation_id}/auto-name")
    def auto_name_conversation(conversation_id: str):
        try:
            title = conversation_titles.rename_now(agent_id, conversation_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        if not title:
            raise HTTPException(status_code=404, detail="nothing to name this conversation from yet")
        return {"id": conversation_id, "title": title}

    add_workspace_routes(router, agent_id)

    return router


def sort_conversations(items: list[dict]) -> list[dict]:
    """Control Panel → Sorting. The vault lists newest first."""
    if service_settings.load()["sort_conversations"] == "alphabetical":
        return sorted(items, key=lambda c: (c.get("title") or "").casefold())
    return items
