"""OpenClave — single FastAPI app serving every agent.

One agent = one entry in agents.AGENTS, not one service. Routes for each agent
are generated from the same router factory (build_agent_router) rather than
hand-written per agent — this is what replaced the reba-service/fresh-service
copy-paste split from 2026-09-04.
"""
import base64
import json
import mimetypes
import os
import tempfile
import io
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError

from config import CONFIG
import agent_factory
import access
from urllib.parse import quote
import agent_order
import purge
import channels
import vault_access
import agent_tools
import avatar_maker
import deploy_state
from version import VERSION
import migrations
import phone_setup
import updater
from urllib.parse import urlsplit
import model_discovery
import model_updater
import provider_credits
import service_settings
import tools_doc
import llm_client
import llm_models
import request_phase
import reminders
import scheduled_tasks
import skills
import catalog
import svg_avatar
import attention
import claude_auth
import cleanup
import connections
import web_push
import vault
import chat_jobs
import conversation_titles
import recaps
import folders
import proposals
import audit
import agent_messages
import room
import claves
import custom_pages
import page_history
import chat_service
import handover
import voice_engine
import setup_check
import agents as agents_module
from agents import AGENTS
from schemas import (
    AgentColorRequest,
    RoomPost, CreateClaveRequest, UpdateClaveRequest, PlaceAgentRequest, RemoveFromClaveRequest,
    ClavesEnabledRequest, AgentClaveRequest, CloneAgentRequest,
    ClaudeLoginCode,
    AvatarFetchRequest, CleanupAction, UnarchiveRequest,
    AgentSummary, AgentUsage, AllUsageResponse, CreateAgentRequest, IdentityBodyRequest, WorkspaceFileCreate, WorkspaceFileRename, WorkspaceFileWrite, MoveWorkspaceFileRequest, RenameAgentRequest, SetAgentToolsRequest, AttachmentUploadResponse, BacklogResponse, ChatRequest,
    ChatRequestCreate, ChatRequestStatus, ChatResponse, ConversationDetail, ConversationSummary, InboxItem,
    InboxResponse, KanbanResponse, KanbanItem, LogMeasurementRequest,
    LogMeasurementsRequest, ModelOption, PlanResponse,
    HandoverRequest, PageAddRequest, MoveConversationRequest, RenameConversationRequest, SetAgentOrderRequest, FolderRequest, SetKanbanStageRequest, SetModelRequest,
    TrackingResponse, UpdateInboxItemRequest, UploadAttachmentRequest, UsageResponse, UsageWindow,
    VoiceReplyRequest, VoiceTranscribeRequest, VoiceTranscribeResponse, VoiceTurnResponse,
)
from routes.common import VOICE_ENABLED_AGENTS, STATIC_DIR, _lan_address, _clave_call, _join_clave, _skill_agent
from routes.library import _avatar_file, _avatar_version
from routes import library as library_routes
from routes.pages import _agent_shell_html
from routes import pages as page_routes
from routes.models import _model_option, _picker_options, _set_model
from routes import models as model_routes
from routes import review as review_routes
from routes import setup as setup_routes
from routes import tools as tool_routes
from routes import push as push_routes
from routes.conversations import api_agent_recap_write
from routes import conversations as conversation_routes
from routes import claves as clave_routes
from routes import agents as agent_routes

app = FastAPI(title="OpenClave")


class NoCacheStaticFiles(StaticFiles):
    """Always revalidate by default. This app changes often and is served to
    a small, trusted set of clients (a phone, a browser tab) — the caching
    bugs this avoids (stale JS/CSS/icons surviving a hard refresh) are worth
    far more here than the request-count savings would be.

    Exception: a request carrying a `v=` query param — a content-version
    cache-buster (see `_asset_version` / mobile_dashboard) — is safe to cache
    aggressively and long, since the URL itself changes whenever the file
    does. UX.md flagged "mobile: takes long to load conversations"; the
    actual API calls measured 2-3ms each, but app.js/styles.css were being
    fully re-downloaded from scratch on every single app open under the
    blanket no-store policy. This fixes that specific, real cost without
    reintroducing the stale-asset risk for anything not explicitly
    versioned.
    """

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        versioned = b"v=" in scope.get("query_string", b"")
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if versioned else "no-store"
        )
        return response


@app.on_event("startup")
def run_data_migrations():
    # First startup hook, so the data is in the current format before chat
    # jobs, reminders and the other background jobs read it. Never raises.
    migrations.run(CONFIG)


@app.on_event("startup")
def recover_chat_jobs():
    chat_jobs.recover()
    chat_jobs.start_kicker()  # runs messages agents queue from their MCP server


@app.on_event("startup")
def refresh_tools_doc():
    # Keeps Build Docs/tools overview.md matching the running tool definitions.
    # Documentation only — never allowed to block startup.
    try:
        tools_doc.write_if_changed()
    except Exception as exc:
        print(f"tools overview note not regenerated: {exc}")


@app.on_event("startup")
def upgrade_agent_pages():
    # Stock copies of the agent page template follow template changes
    # (agent_factory.upgrade_stock_dashboards); custom ones are left alone.
    if os.environ.get("OPENCLAVE_DISABLE_BACKGROUND_JOBS", "").strip().lower() in {"1", "true", "yes"}:
        return
    try:
        upgraded = agent_factory.upgrade_stock_dashboards()
        if upgraded:
            print(f"agent pages upgraded to the current template: {', '.join(upgraded)}")
    except Exception as exc:
        print(f"agent page upgrade skipped: {exc}")


@app.on_event("startup")
def start_reminders():
    if os.environ.get("OPENCLAVE_DISABLE_BACKGROUND_JOBS", "").strip().lower() in {"1", "true", "yes"}:
        return
    reminders.start()
    scheduled_tasks.start()
    attention.start()
    cleanup.start()  # Weekly maintainer cleanup suggestions.
    recaps.backfill(list(AGENTS))  # cards for archived chats that have none yet
    model_updater.start()  # first run 15 min after startup, then daily; background only
    updater.start()  # [updates] check: git fetch at most every 6 hours, background only


@app.on_event("startup")
def start_voice_engine():
    # Non-fatal: voice is optional and every other route must work when its
    # separate interpreter or model directory is not configured.
    try:
        voice_engine.start()
    except Exception as exc:
        print(f"voice engine failed to start, voice endpoints will error until fixed: {exc}")


@app.on_event("shutdown")
def stop_voice_engine():
    voice_engine.stop()


# Backlog.md item #3: agent-editable dashboard.html used to be served directly
# on this app's own origin, so any agent's dashboard JS could call any other
# agent's API (including R2's Home-Assistant action tools) as if it were the owner.
# The real containment is the sandboxed iframe in static/dashboard_shell.js —
# an opaque-origin iframe with no allow-same-origin cannot make the browser
# send/read a same-origin request at all, so it has no path to the network
# except the narrow, agent-id-checked postMessage bridge that script owns.
# This check is defense in depth for everything else on this origin (a
# genuinely cross-site page, or a future change that weakens the sandbox).


def _trusted_origins() -> set[str]:
    """Origins allowed to call this instance's API from a browser."""
    from urllib.parse import urlsplit

    origins = set()
    if CONFIG.public_url:
        public = urlsplit(CONFIG.public_url)
        if public.scheme and public.netloc:
            origins.add(f"{public.scheme}://{public.netloc}")

    origins.update(CONFIG.extra_origins)
    port = CONFIG.port
    bind = CONFIG.bind_address.strip().lower()
    if bind in {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}:
        origins.update({f"http://127.0.0.1:{port}", f"http://localhost:{port}"})
    if bind in {"0.0.0.0", "::"} and (lan := _lan_address()):
        origins.add(f"http://{lan}:{port}")
    elif bind:
        host = f"[{bind}]" if ":" in bind and not bind.startswith("[") else bind
        origins.add(f"http://{host}:{port}")
    return origins


TRUSTED_ORIGINS = _trusted_origins()


@app.middleware("http")
async def require_access_token(request: Request, call_next):
    """access.py: off unless an access token is configured."""
    if access.allowed(request.client.host if request.client else None, request.headers, request.cookies, request.url.path):
        return await call_next(request)
    if access.wants_html(request.headers, request.method):
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
    return JSONResponse(status_code=401, content={"detail": "sign in first (open /login)"})


@app.middleware("http")
async def enforce_trusted_origin(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin is not None and origin not in TRUSTED_ORIGINS:
        return JSONResponse(status_code=403, content={"detail": "cross-origin request blocked"})
    return await call_next(request)


# Host header check against DNS rebinding: a web page whose DNS name is
# switched to 127.0.0.1 (or this machine's LAN address) becomes "same origin"
# with OpenClave, sends no Origin header on GETs and could read the API. Its
# requests still carry the page's own name in Host, so only names OpenClave
# knows are served: loopback names, the hosts of TRUSTED_ORIGINS (public_url,
# extra_origins, the bind and LAN addresses) and any IP literal (rebinding
# needs a DNS name). Ports are ignored, so a Docker port mapping or a
# reverse proxy on another port still matches. `tailscale serve` passes the
# public name through as Host, which is the public_url host.
# "testserver" is the Host Starlette's TestClient sends; it never travels a network.
_LOOPBACK_HOSTS = {"localhost", "testserver"}


def _host_name(host: str) -> str:
    from urllib.parse import urlsplit

    try:
        return (urlsplit(f"//{host.strip()}").hostname or "").rstrip(".")
    except ValueError:
        return ""


def _trusted_hosts(origins: set[str]) -> set[str]:
    from urllib.parse import urlsplit

    return _LOOPBACK_HOSTS | {(urlsplit(o).hostname or "").rstrip(".") for o in origins} - {""}


def host_allowed(host: str | None, trusted: set[str] | None = None) -> bool:
    import ipaddress

    name = _host_name(host or "")
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name in (trusted if trusted is not None else _trusted_hosts(TRUSTED_ORIGINS)) or name.endswith(".localhost")


@app.middleware("http")
async def enforce_trusted_host(request: Request, call_next):
    host = request.headers.get("host")
    if not host_allowed(host):
        name = _host_name(host or "") or "(none)"
        return JSONResponse(status_code=421, content={"detail": (
            f"OpenClave doesn't answer to the address '{name}'. If this is your own address for it, "
            f"add it to openclave.toml: [network] public_url = \"https://{name}\" (the address you open "
            f"it at), or extra_origins = [\"http://{name}:{CONFIG.port}\"]; then restart OpenClave.")})
    return await call_next(request)


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

    # The agent's own phone page (custom_pages.py): page.json resolved
    # against its files on every load, so it's as current as they are.
    @router.get("/page")
    def get_page():
        # Record a page the agent changed since (page_history.py), so the
        # phone can offer Keep / Revert before the owner relies on it.
        page_history.safe_snapshot(agent_id)
        return {"page": custom_pages.load(agent_id), "changed": page_history.pending(agent_id)}

    @router.post("/page/keep")
    def keep_page():
        page_history.keep(agent_id)
        return {"changed": None}

    @router.post("/page/revert")
    def revert_page():
        try:
            result = page_history.restore(agent_id, "phone", note="The user tapped Revert")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {**result, "page": custom_pages.load(agent_id), "changed": page_history.pending(agent_id)}

    # The page's form block (custom_pages.add_item): The owner types one line,
    # it's appended to the note that block names. Only the block number
    # comes from the browser; the path comes from the saved page.json.
    @router.post("/page/add")
    def add_page_item(req: PageAddRequest):
        try:
            return custom_pages.add_item(agent_id, req.block, req.text)
        except custom_pages.PageError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.get("/page/html/{rel_path:path}")
    def get_page_html(rel_path: str):
        try:
            f = custom_pages.html_file(agent_id, rel_path)
        except custom_pages.PageError:
            f = None
        if f is None or not f.is_file():
            raise HTTPException(status_code=404, detail="no such page")
        # Agent-written: shown in a sandboxed frame, and even opened directly
        # it can't reach the network or this origin.
        return FileResponse(f, media_type="text/html", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
                                       "style-src 'unsafe-inline'; img-src data:; font-src data:",
        })

    @router.get("/plan", response_model=PlanResponse)
    def get_plan():
        return vault.read_plan(agent_id)

    @router.get("/backlog", response_model=BacklogResponse)
    def get_backlog():
        return vault.read_backlog(agent_id)

    @router.get("/kanban", response_model=KanbanResponse)
    def get_kanban():
        stages = vault.read_kanban(agent_id)
        return KanbanResponse(items=[KanbanItem(text=t, stage=s) for t, s in stages.items()])

    @router.post("/kanban")
    def set_kanban(req: SetKanbanStageRequest):
        if req.stage is not None and req.stage not in vault.KANBAN_STAGES:
            raise HTTPException(status_code=400, detail="invalid stage")
        vault.set_kanban_stage(agent_id, req.item, req.stage)
        return {"item": req.item, "stage": req.stage}

    @router.get("/usage", response_model=UsageResponse)
    def get_usage():
        windows = llm_client.usage_summary(agent_id)
        return UsageResponse(windows={k: UsageWindow(**v) for k, v in windows.items()})

    @router.get("/tracking", response_model=TrackingResponse)
    def get_tracking(metric: str | None = None, limit: int | None = None, section: str = "Progress"):
        return vault.read_measurements(agent_id, metric, limit, section)

    @router.post("/tracking", response_model=TrackingResponse)
    def post_tracking(req: LogMeasurementRequest):
        # Deterministic, no LLM call — for dashboard buttons/quick-log UI, where a
        # button press shouldn't cost a real model call or wait on one (Build
        # Requirements §2.3: deterministic-first for structured/repetitive tasks).
        vault.log_measurement(agent_id, req.metric, req.value, req.unit, req.note, req.date, req.section)
        return vault.read_measurements(agent_id, section=req.section)

    @router.post("/tracking/batch", response_model=TrackingResponse)
    def post_tracking_batch(req: LogMeasurementsRequest):
        # Logging several metrics at once (e.g. the dashboard's Log button)
        # MUST go through here, one request/one write, rather than one POST
        # /tracking call per metric — concurrent single-entry writes race on
        # tracking.md and silently drop each other's entries.
        vault.log_measurements(agent_id, [e.model_dump() for e in req.entries], req.section)
        return vault.read_measurements(agent_id, section=req.section)

    @router.get("/dashboard-src")
    def get_dashboard_src():
        # Raw HTML as data, fetched by this agent's trusted shell page and
        # assigned to a sandboxed iframe's srcdoc — never served/navigated to
        # directly as a page (plain text, not text/html) so it never executes
        # outside that sandbox.
        return PlainTextResponse(vault.read_dashboard(agent_id))

    @router.get("/avatar-data")
    def get_avatar_data():
        # The header icon on the agent page: the same picture as the dashboard
        # tile, as a data URL because the sandboxed page only reaches
        # "/<agent>/" through the shell's JSON bridge. None = keep the initials.
        path = _avatar_file(agent_id)
        if not path:
            return {"data_url": None}
        if path.suffix == ".svg":
            raw = svg_avatar.sanitize(path.read_text(encoding="utf-8")).encode("utf-8")
            mime = "image/svg+xml"
        else:
            raw, mime = path.read_bytes(), "image/png"
        return {"data_url": f"data:{mime};base64," + base64.b64encode(raw).decode()}

    # Files tab on the agent page: The owner's quick edits to the agent's own
    # workspace files. Agent-scoped so the sandboxed dashboard can reach it.
    @router.get("/files")
    def list_files():
        return vault.list_workspace_files(agent_id)

    @router.post("/files", status_code=201)
    def create_file(req: WorkspaceFileCreate):
        try:
            return vault.create_workspace_file(agent_id, req.path)
        except (ValueError, vault.WriteScopeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except FileExistsError:
            raise HTTPException(status_code=409, detail="A file with that name already exists")

    @router.post("/files/rename")
    def rename_file(req: WorkspaceFileRename):
        try:
            return vault.rename_workspace_file(agent_id, req.path, req.new_path)
        except (ValueError, vault.WriteScopeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="File not found")
        except FileExistsError:
            raise HTTPException(status_code=409, detail="A file with that name already exists")

    @router.delete("/files/content")
    def delete_file(path: str):
        try:
            vault.delete_workspace_file(agent_id, path)
        except (ValueError, vault.WriteScopeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="File not found")
        return {"deleted": path}

    @router.get("/files/content")
    def read_file(path: str):
        try:
            f = vault.read_workspace_file(agent_id, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if f is None:
            raise HTTPException(status_code=404, detail="File not found")
        return f

    @router.put("/files/content")
    def write_file(req: WorkspaceFileWrite):
        try:
            return vault.write_workspace_file(agent_id, req.path, req.content, req.version)
        except (ValueError, vault.WriteScopeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="File not found")
        except vault.NoteConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @router.get("/model", response_model=ModelOption)
    def get_agent_model():
        # Agent-scoped on purpose: a dashboard running in the sandboxed iframe
        # can only reach its own "/<agent>/" prefix (dashboard_shell.js's
        # bridge), so this exists specifically so injectAttachUI's vision
        # check doesn't need the global /api/agents + /api/models endpoints,
        # which that bridge correctly refuses (that refusal was the actual
        # bug behind the attach button never appearing).
        model = llm_models.get(llm_models.selected_model_id(agent_id))
        return _model_option(model)

    @router.get("/models", response_model=list[ModelOption])
    def list_agent_models():
        # Agent-scoped twin of /api/models, same reasoning as get_agent_model
        # above: the sandboxed dashboard iframe can't reach /api/models
        # directly, so the desktop model selector (dashboard_common.js) needs
        # its own catalog fetch under this agent's own prefix.
        return _picker_options(agent_id)

    @router.get("/model-response-times")
    def model_response_times():
        return vault.read_response_time_averages(agent_id)

    @router.post("/model")
    def set_agent_model(req: SetModelRequest):
        # Agent-scoped twin of POST /api/agents/{agent_id}/model, for the same
        # sandboxed-iframe reason as list_agent_models above.
        return _set_model(agent_id, req)

    return router


def _register_agent_routes(agent_id: str) -> None:
    """API routes plus the agent's page. Called for every agent at startup and
    for a newly created one straight away, so it works without a restart."""
    VOICE_ENABLED_AGENTS.add(agent_id)
    app.include_router(build_agent_router(agent_id), prefix=f"/{agent_id}")
    app.get(f"/{agent_id}")(lambda: HTMLResponse(_agent_shell_html(agent_id)))


def sort_conversations(items: list[dict]) -> list[dict]:
    """Control Panel → Sorting. The vault lists newest first."""
    if service_settings.load()["sort_conversations"] == "alphabetical":
        return sorted(items, key=lambda c: (c.get("title") or "").casefold())
    return items


app.include_router(library_routes.router)
app.include_router(page_routes.router)
app.include_router(model_routes.router)
app.include_router(review_routes.router)
app.include_router(setup_routes.router)
app.include_router(tool_routes.router)
app.include_router(push_routes.router)
app.include_router(conversation_routes.router)
app.include_router(clave_routes.router)
app.include_router(agent_routes.router)


for _agent_id in list(AGENTS):
    _register_agent_routes(_agent_id)


app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")
