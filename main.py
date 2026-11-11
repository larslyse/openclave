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

# Every registered agent shares the voice pipeline.
VOICE_ENABLED_AGENTS = set(AGENTS)

app = FastAPI(title="OpenClave")
STATIC_DIR = Path(__file__).parent / "static"
AVATAR_DIR = CONFIG.state_path("custom_avatars")


def _avatar_file(agent_id: str):
    """The image the avatar endpoint serves (upload or agent-owned asset)."""
    cfg = AGENTS.get(agent_id)
    agent_root = agents_module.AGENTS_ROOT / cfg.folder if cfg else None
    paths = [AVATAR_DIR / f"{agent_id}.svg", AVATAR_DIR / f"{agent_id}.png"]
    if agent_root:
        paths.extend((agent_root / "avatar.svg", agent_root / "avatar.png"))
    for path in paths:
        if path.exists():
            return path
    return None


def _avatar_version(agent_id: str) -> int:
    """Cache-buster for `/api/agents/<id>/avatar?v=`; 0 = generated initials (not cached)."""
    path = _avatar_file(agent_id)
    return int(path.stat().st_mtime) if path else 0


@app.get("/api/agents/{agent_id}/avatar")
def agent_avatar(agent_id: str, v: int = 0):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    path = _avatar_file(agent_id)
    # With a matching ?v= (from /api/agents) the URL changes whenever the image
    # does, so the phone may keep it; anything else stays no-store.
    cache = "public, max-age=31536000, immutable" if path and v and v == _avatar_version(agent_id) else "no-store"
    if path and path.parent == AVATAR_DIR and path.suffix == ".svg":
        # Agent-drawn at onboarding: sanitized on save (svg_avatar.py); the CSP
        # blocks scripts even if the file is opened directly as a page.
        return FileResponse(path, media_type="image/svg+xml", headers={
            "Cache-Control": cache, "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        })
    if path:
        return FileResponse(path, headers={"Cache-Control": cache})
    # Agents created from the Control Panel: initials on their color until an image is uploaded.
    cfg = AGENTS[agent_id]
    initials = agent_factory._initials(cfg.name)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="32" fill="{cfg.color}"/>'
           f'<text x="32" y="41" text-anchor="middle" font-family="sans-serif" font-size="24" font-weight="600" fill="#121517">{initials}</text></svg>')
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.post("/api/agents/{agent_id}/avatar")
async def set_agent_avatar(agent_id: str, request: Request, tint: bool = False):
    """Save an uploaded picture as the avatar. With ?tint=1 the picture is
    shown in the agent's colour, and the untinted one is kept as
    <id>.base.png so a later colour change re-tints it."""
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    raw = await request.body()
    if len(raw) > 2_000_000:
        raise HTTPException(status_code=413, detail="Image must be under 2 MB")
    try:
        image = Image.open(io.BytesIO(raw))
        if image.width > 4096 or image.height > 4096 or image.width < 1 or image.height < 1:
            raise ValueError("Invalid image dimensions")
        image.load()
        image = image.convert("RGBA")
        image.thumbnail((256, 256))
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        raise HTTPException(status_code=400, detail="Upload a valid image")
    AVATAR_DIR.mkdir(exist_ok=True)
    target = AVATAR_DIR / f"{agent_id}.png"
    with tempfile.NamedTemporaryFile(dir=AVATAR_DIR, suffix=".png", delete=False) as output:
        temp_path = Path(output.name)
    base = AVATAR_DIR / f"{agent_id}.base.png"
    try:
        if tint:
            image.save(base, format="PNG")
            image = avatar_maker.tint(image, AGENTS[agent_id].color)
        else:
            base.unlink(missing_ok=True)
        image.save(temp_path, format="PNG")
        os.replace(temp_path, target)
    except avatar_maker.AvatarError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        temp_path.unlink(missing_ok=True)
    (AVATAR_DIR / f"{agent_id}.svg").unlink(missing_ok=True)  # an upload replaces an agent-drawn avatar
    return {"ok": True}


# Avatar library (catalog/avatars): ready pictures to pick instead of the initials.
_SVG_HEADERS = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"}


@app.get("/api/avatar-library")
def api_avatar_library():
    return {"avatars": catalog.avatar_library()}


@app.get("/api/avatar-library/{avatar_id}.svg")
def api_avatar_library_svg(avatar_id: str):
    try:
        svg = svg_avatar.sanitize(catalog.library_avatar(avatar_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(svg, media_type="image/svg+xml", headers={**_SVG_HEADERS, "Cache-Control": "public, max-age=3600"})


@app.post("/api/agents/{agent_id}/avatar/library")
def api_pick_library_avatar(agent_id: str, req: dict):
    """{avatar: id} -> that library picture becomes the agent's avatar."""
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        agent_factory.save_svg_avatar(agent_id, catalog.library_avatar(str(req.get("avatar") or "")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


# Avatar maker (Control Panel → Edit → Make avatar…, static/avatar_maker.js):
# the browser crops; the server fetches remote pictures and cartoonifies.
@app.post("/api/avatar/fetch")
def avatar_fetch(req: AvatarFetchRequest):
    """A picture from a public URL, as a PNG data URL the editor can draw."""
    try:
        image = avatar_maker.fetch_image(req.url)
    except avatar_maker.AvatarError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"data_url": "data:image/png;base64," + base64.b64encode(avatar_maker.to_png(image)).decode(),
            "width": image.width, "height": image.height}


@app.post("/api/avatar/cartoon")
async def avatar_cartoon(request: Request, style: str = "cartoon", strength: int = 2, color: str = ""):
    """The cropped picture (request body) in a style ("original" for none),
    tinted in `color` (#RRGGBB) if given, as a PNG."""
    raw = await request.body()
    if len(raw) > 4_000_000:
        raise HTTPException(status_code=413, detail="Image must be under 4 MB")
    try:
        png = avatar_maker.cartoon_png(raw, style, strength, color or None)
    except avatar_maker.AvatarError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


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


def _asset_version(*relative_parts: str) -> int:
    """mtime-based cache-buster for one static file, in seconds — changes
    exactly when the file does, no content hashing needed at this scale."""
    return int((STATIC_DIR.joinpath(*relative_parts)).stat().st_mtime)


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
def _lan_address() -> str | None:
    """This machine's address on its local network (the interface a packet
    to the internet would leave by; nothing is sent)."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # TEST-NET address: routing lookup only
            address = sock.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") else address


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


@app.get("/login", response_class=HTMLResponse)
def login_form(next: str = "/"):
    return HTMLResponse(access.login_page(next))


@app.post("/login")
async def login_submit(request: Request, next: str = "/"):
    form = await request.form()
    if not access.token_ok(str(form.get("token") or "")):
        return HTMLResponse(access.login_page(next, "That token isn't right."), status_code=401)
    safe = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(safe, status_code=303)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(**access.cookie_args(secure))
    return response


@app.middleware("http")
async def enforce_trusted_origin(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin is not None and origin not in TRUSTED_ORIGINS:
        return JSONResponse(status_code=403, content={"detail": "cross-origin request blocked"})
    return await call_next(request)


def _model_option(m, agent_id: str | None = None, times: dict | None = None,
                  shown: list | None = None) -> ModelOption:
    price_in, price_out = llm_models.api_prices(m)
    t = (times or {}).get(m.id) or {}
    cost = llm_models.cost_tier(m)
    label, color = llm_models.COST_TIERS.get(cost, ("", ""))
    return ModelOption(
        id=m.id, provider=m.provider, display_name=m.display_name, tier=m.tier,
        supports_vision=m.supports_vision, api_input_usd=price_in, api_output_usd=price_out,
        route=m.route, speed=m.speed, group=llm_models.group(m),
        supports=connections.supported(agent_id, m.id) if agent_id else [],
        cost_tier=cost, cost_label=label, cost_color=color,
        vendor=llm_models.vendor(m), short_name=llm_models.short_name(m), released=m.released,
        released_is_added=llm_models.released_is_added(m), old=llm_models.old_reason(m, shown),
        latency_ms=t.get("average_ms"), latency_samples=t.get("samples", 0),
    )


def _picker_options(agent_id: str | None = None) -> list[ModelOption]:
    shown = llm_models.picker_models()
    times = api_model_response_times()
    ready = setup_check.ready_providers()
    return [_model_option(m, agent_id, times, shown).model_copy(update={"ready": m.provider in ready}) for m in shown]


def _set_model(agent_id: str, req: SetModelRequest) -> dict:
    """The one way a model gets chosen (every picker ends here). No blocking
    warning: pickers tag models that support the agent's connections, and the
    agent itself says so if it's on one that doesn't (connections.py)."""
    if req.model_id not in {m.id for m in llm_models.CATALOG}:
        raise HTTPException(status_code=400, detail="unknown model id")
    llm_models.set_selected_model(agent_id, req.model_id)
    return {"agent_id": agent_id, "model_id": req.model_id, "loses": connections.lost(agent_id, req.model_id)}


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


@app.get("/api/maintainer/inbox", response_model=InboxResponse)
def maintainer_inbox(status: str | None = None):
    return InboxResponse(items=[InboxItem(**i) for i in vault.read_maintainer_inbox(status)])


@app.get("/api/maintainer/inbox/{item_id}", response_model=InboxItem)
def maintainer_inbox_item(item_id: str):
    item = vault.get_maintainer_inbox_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="inbox item not found")
    return InboxItem(**item)


@app.post("/api/maintainer/inbox/{item_id}", response_model=InboxItem)
def update_maintainer_inbox_item(item_id: str, req: UpdateInboxItemRequest):
    # Deterministic, no LLM call — same reasoning as POST /<agent>/tracking:
    # a dashboard acknowledge/resolve button shouldn't need a chat round trip.
    try:
        updated = vault.update_maintainer_inbox_item(item_id, req.status, req.result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not updated:
        raise HTTPException(status_code=404, detail="inbox item not found")
    return InboxItem(**vault.get_maintainer_inbox_item(item_id))


# ── Reminders (agents' scheduled messages) and Web Push ──
@app.get("/api/reminders")
def api_reminders():
    """Control Panel overview: every agent's upcoming reminders and recent sends."""
    out = []
    for agent_id in agent_order.get_order():
        upcoming = reminders.list_for(agent_id)
        sent = reminders.history_for(agent_id)[:10]
        if upcoming or sent:
            out.append({"agent_id": agent_id, "agent_name": AGENTS[agent_id].name, "upcoming": upcoming, "sent": sent})
    return {"now": reminders.describe_now(), "agents": out}


@app.delete("/api/reminders/{agent_id}/{reminder_id}")
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


@app.get("/api/tasks")
def api_tasks():
    """Control Panel overview: every agent's scheduled tasks with their last run."""
    return scheduled_tasks.overview()


@app.post("/api/tasks/{agent_id}")
def api_add_task(agent_id: str, req: dict):
    _task_agent(agent_id)
    try:
        return scheduled_tasks.add(agent_id, req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/tasks/{agent_id}/{task_id}")
def api_update_task(agent_id: str, task_id: str, req: dict):
    _task_agent(agent_id)
    try:
        task = scheduled_tasks.update(agent_id, task_id, req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.delete("/api/tasks/{agent_id}/{task_id}")
def api_delete_task(agent_id: str, task_id: str):
    _task_agent(agent_id)
    if not scheduled_tasks.delete(agent_id, task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    return {"deleted": task_id}


@app.post("/api/tasks/{agent_id}/{task_id}/run")
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


# ── Ready-made agents (catalog.py) ──
@app.get("/api/catalog")
def api_catalog():
    return {"templates": catalog.listing()}


@app.get("/api/catalog/{template_id}/avatar")
def api_catalog_avatar(template_id: str):
    try:
        t = catalog.get(template_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not t["has_avatar"]:
        raise HTTPException(status_code=404, detail="no avatar")
    svg = svg_avatar.sanitize((t["folder"] / "avatar.svg").read_text(encoding="utf-8"))
    return Response(svg, media_type="image/svg+xml", headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})


@app.post("/api/catalog/{template_id}/install")
def api_catalog_install(template_id: str, req: dict):
    """{model_id, name?, persona?, extras?, mission_extra?} -> the new agent, in onboarding."""
    extras = req.get("extras") or []
    try:
        if not isinstance(extras, list) or not all(isinstance(x, str) for x in extras):
            raise ValueError("extras must be a list of extra job ids")
        created = catalog.install(template_id, str(req.get("model_id") or ""), req.get("name") or None,
                                  persona=req.get("persona") or None, mission_extra=req.get("mission_extra") or None,
                                  extras=extras)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _register_agent_routes(created["id"])
    _join_clave(created["id"], req.get("clave"))
    return created


# ── Recaps (recaps.py): archived conversations as short cards ──
@app.get("/api/agents/{agent_id}/recaps")
def api_agent_recaps(agent_id: str):
    _skill_agent(agent_id)
    return {"recaps": recaps.list_for(agent_id), "can_write": recaps.available()}


@app.get("/api/agents/{agent_id}/recaps/{conversation_id}")
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


@app.post("/api/agents/{agent_id}/recaps/{conversation_id}/write")
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


@app.post("/api/agents/{agent_id}/recaps/{conversation_id}/restore")
def api_agent_recap_restore(agent_id: str, conversation_id: str):
    _skill_agent(agent_id)
    try:
        if not vault.unarchive_conversation(agent_id, conversation_id):
            raise HTTPException(status_code=404, detail="no such archived conversation")
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"restored": conversation_id}


# ── Skills (skills.py): know-how agents load on demand ──
def _skill_agent(agent_id: str) -> None:
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")


@app.get("/api/agents/{agent_id}/skills")
def api_agent_skills(agent_id: str):
    _skill_agent(agent_id)
    return {"skills": skills.list_for(agent_id)}


@app.get("/api/agents/{agent_id}/skills/{scope}/{name}")
def api_skill_text(agent_id: str, scope: str, name: str):
    _skill_agent(agent_id)
    try:
        return {"key": f"{scope}/{name}", "text": skills.text(agent_id, f"{scope}/{name}")}
    except (skills.SkillError, OSError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/agents/{agent_id}/skills/{scope}/{name}")
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


@app.delete("/api/agents/{agent_id}/skills/{scope}/{name}")
def api_skill_delete(agent_id: str, scope: str, name: str):
    _skill_agent(agent_id)
    try:
        skills.delete(agent_id, f"{scope}/{name}")
    except skills.SkillError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"deleted": f"{scope}/{name}"}


@app.post("/api/skills/import")
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


@app.get("/api/push/key")
def api_push_key():
    return {"key": web_push.public_key()}


@app.post("/api/push/subscribe")
def api_push_subscribe(req: dict):
    try:
        web_push.add_subscription(req.get("subscription") or {}, str(req.get("label") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"devices": len(web_push.subscriptions())}


@app.post("/api/push/unsubscribe")
def api_push_unsubscribe(req: dict):
    web_push.remove_subscription(str(req.get("endpoint") or ""))
    return {"devices": len(web_push.subscriptions())}


@app.get("/api/push/status")
def api_push_status():
    return {"devices": [{"label": s.get("label", ""), "added": s.get("added", "")} for s in web_push.subscriptions()]}


@app.post("/api/push/test")
def api_push_test():
    delivered = web_push.send_all("OpenClave", "Test notification: reminders will arrive like this.",
                                  url="/mobile/", tag="openclave-test")
    return {"delivered": delivered, "devices": len(web_push.subscriptions())}


@app.post("/api/files/move")
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


@app.post("/api/conversations/move")
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


@app.post("/api/conversations/handover")
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


@app.get("/api/room")
def api_room(after: int = 0, clave: str = claves.EVERYONE):
    try:
        return room.messages(after, clave=clave)
    except KeyError:
        raise HTTPException(status_code=404, detail="Room not found")


@app.post("/api/room")
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


@app.get("/api/channels")
def api_channels():
    return channels.list_channels()


@app.post("/api/channels")
def api_channel_create(req: dict):
    return _channel_call(channels.create, str(req.get("name", "")), req.get("members") or [])


@app.patch("/api/channels/{channel_id}")
def api_channel_update(channel_id: str, req: dict):
    name = req.get("name")
    return _channel_call(channels.update, channel_id, str(name) if name is not None else None, req.get("members"))


@app.delete("/api/channels/{channel_id}")
def api_channel_delete(channel_id: str):
    _channel_call(channels.delete, channel_id, room._file(channel_id))
    return {"deleted": channel_id}


# ── Claves (claves.py): groups of agents, each with its own group chat ──
def _clave_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Not found: {exc.args[0] if exc.args else ''}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/claves")
def api_claves():
    return claves.overview()


@app.post("/api/claves/enabled")
def api_claves_enabled(req: ClavesEnabledRequest):
    service_settings.update({"multiple_claves": req.enabled})
    if req.enabled:
        claves.ensure_default(agent_order.get_order())
    return claves.overview()


@app.post("/api/claves")
def api_create_clave(req: CreateClaveRequest):
    _clave_call(claves.create, req.name, req.members)
    return claves.overview()


@app.patch("/api/claves/{clave_id}")
def api_update_clave(clave_id: str, req: UpdateClaveRequest):
    kwargs = {"name": req.name, "members": req.members}
    if req.set_lead:
        kwargs["lead"] = req.lead or None
    _clave_call(claves.update, clave_id, **kwargs)
    return claves.overview()


@app.delete("/api/claves/{clave_id}")
def api_archive_clave(clave_id: str):
    _clave_call(claves.archive, clave_id)
    return claves.overview()


@app.post("/api/claves/active")
def api_active_clave(req: dict):
    _clave_call(claves.set_active, str(req.get("clave") or ""))
    return claves.overview()


@app.post("/api/claves/place")
def api_place_agent(req: PlaceAgentRequest):
    _clave_call(claves.place, req.agent, req.to, index=req.index, lead=req.lead)
    return claves.overview()


@app.post("/api/claves/clone")
def api_clone_agent(req: CloneAgentRequest):
    """The same agent for another clave, with its own empty memory and chats."""
    if req.agent not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    created = _clave_call(agent_factory.clone_agent, req.agent, req.to)
    _register_agent_routes(created["id"])
    return {**created, **claves.overview()}


@app.post("/api/claves/remove")
def api_remove_from_clave(req: RemoveFromClaveRequest):
    _clave_call(claves.remove, req.agent, req.clave)
    return claves.overview()


@app.get("/api/agents/{agent_id}/claves")
def api_agent_claves(agent_id: str):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"enabled": claves.enabled(), "selected": claves.clave_of(agent_id),
            "manager": agent_id == agents_module.manager_id(),
            "claves": [{"id": c.id, "name": c.name} for c in claves.list_claves()]}


@app.post("/api/agents/{agent_id}/claves")
def api_set_agent_clave(agent_id: str, req: AgentClaveRequest):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"selected": _clave_call(claves.set_agent_clave, agent_id, req.clave or None)}


@app.get("/api/agent-messages")
def api_agent_messages():
    return agent_messages.threads()


@app.get("/api/agents", response_model=list[AgentSummary])
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


@app.get("/api/usage", response_model=AllUsageResponse)
def api_usage():
    total = llm_client.usage_summary(None)
    by_agent = [
        AgentUsage(agent_id=agent_id, windows={k: UsageWindow(**v) for k, v in llm_client.usage_summary(agent_id).items()})
        for agent_id in AGENTS
    ]
    return AllUsageResponse(total={k: UsageWindow(**v) for k, v in total.items()}, by_agent=by_agent)


@app.get("/api/replies")
def api_recent_replies(after: int = 0):
    """Replies finished after `after` (ms), oldest first: Autojump polls this."""
    return chat_jobs.recent_replies(after)


@app.post("/api/agents")
def api_create_agent(req: CreateAgentRequest):
    try:
        created = agent_factory.create_agent(req.name, req.purpose, req.model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _register_agent_routes(created["id"])
    _join_clave(created["id"], req.clave)
    return created


def _join_clave(agent_id: str, clave_id) -> None:
    """A new agent made from a clave's "+ New agent" box joins that clave.
    The agent exists by now, so a clave that has gone away is just skipped."""
    if clave_id:
        try:
            claves.place(agent_id, str(clave_id))
        except (KeyError, ValueError):
            pass


@app.get("/api/agents/{agent_id}/profile")
def api_agent_profile(agent_id: str):
    """Everything the Control Panel's Edit panel shows."""
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    cfg = AGENTS[agent_id]
    return {"id": agent_id, "name": cfg.name, "tagline": cfg.tagline, "color": cfg.color,
            "mission": vault.read_identity_body(agent_id, "mission"),
            "persona": vault.read_identity_body(agent_id, "persona"),
            "vault_write": vault_access.folders(agent_id)}


@app.post("/api/agents/{agent_id}/vault-write")
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


@app.post("/api/agents/{agent_id}/name")
def api_rename_agent(agent_id: str, req: RenameAgentRequest):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        return agent_factory.rename_agent(agent_id, req.name, req.tagline)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/agents/{agent_id}/color")
def api_agent_color(agent_id: str, req: AgentColorRequest):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        return agent_factory.set_color(agent_id, req.color)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.put("/api/agents/{agent_id}/identity/{kind}")
def api_set_identity(agent_id: str, kind: str, req: IdentityBodyRequest):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    if kind not in ("mission", "persona"):
        raise HTTPException(status_code=404, detail="Unknown identity file")
    try:
        vault.set_identity_body_by_lars(agent_id, kind, req.body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return vault.read_identity_body(agent_id, kind)


@app.get("/api/tools")
def api_tools():
    """Tool catalog for the Control Panel's per-agent tool editor."""
    return {
        "core": list(agent_tools.CORE_TOOLS),
        "optional": [
            {"name": t["function"]["name"], "summary": t["function"]["description"].split(". ")[0].rstrip(".") + "."}
            for t in llm_client.TOOLS if t["function"]["name"] in agent_tools.OPTIONAL_TOOLS
        ] + [{"name": name, "summary": text.split(". ")[0] + "."} for name, text in agent_tools.CAPABILITIES.items()],
    }


@app.get("/api/agents/{agent_id}/tools")
def api_agent_tools(agent_id: str):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    chosen = agent_tools.selected_optional(agent_id)
    available = list(agent_tools.optional_for(agent_id))
    return {"available": available, "selected": available if chosen is None else chosen,
            "customized": chosen is not None, "onboarding": agents_module.is_onboarding(agent_id),
            "connections": connections.status(agent_id), "connection_hint": connections.route_hint(agent_id),
            "writes": f"Only its own folder (Agents/{AGENTS[agent_id].name}/)"}


@app.post("/api/agents/{agent_id}/tools")
def api_set_agent_tools(agent_id: str, req: SetAgentToolsRequest):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        return {"selected": agent_tools.set_optional(agent_id, req.tools)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def sort_conversations(items: list[dict]) -> list[dict]:
    """Control Panel → Sorting. The vault lists newest first."""
    if service_settings.load()["sort_conversations"] == "alphabetical":
        return sorted(items, key=lambda c: (c.get("title") or "").casefold())
    return items


@app.get("/api/settings")
def api_get_settings():
    return service_settings.load()


@app.get("/api/access")
def api_access():
    """Control Panel → Connections: the addresses this instance answers on."""
    port, bind = CONFIG.port, CONFIG.bind_address.strip().lower()
    rows = [{"name": "This computer", "url": f"http://localhost:{port}/", "kind": "Local"},
            {"name": "This computer (phone app)", "url": f"http://localhost:{port}/mobile/", "kind": "Local"}]
    lan = _lan_address() if bind in {"0.0.0.0", "::"} else None
    if lan:
        rows.append({"name": "Other devices on the network", "url": f"http://{lan}:{port}/", "kind": "LAN"})
    if CONFIG.public_url:
        rows += [{"name": "Secure address", "url": f"{CONFIG.public_url}/", "kind": "Public"},
                 {"name": "Secure address (phone app)", "url": f"{CONFIG.public_url}/mobile/", "kind": "Public"}]
    tunnel = f"ssh -L {port}:localhost:{port} <user>@{lan}" if lan else ""
    return {"rows": rows, "tunnel": tunnel}


@app.get("/api/network")
def api_network():
    """Network values needed by the mobile client, without exposing settings."""
    return {
        "secure_mobile_url": f"{CONFIG.public_url}/mobile/" if CONFIG.public_url else "",
        "voice_available": voice_engine.available(),
    }


@app.post("/api/settings")
def api_update_settings(changes: dict):
    try:
        return service_settings.update(changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/attention")
def api_attention():
    """Per-agent findings (attention.py) plus the digest's last run."""
    results = attention.check_all()
    return {"agents": results, "needs_you": attention.needs_you(results), "digest": attention.state()}


@app.post("/api/attention/digest-test")
def api_attention_digest_test():
    """Send the digest now, even if empty, to check it arrives."""
    return attention.send_digest(force=True)


# ── Weekly cleanup (cleanup.py): Rick suggests, the owner applies ──

# Claude CLI sign-in (claude_auth.py): status, start, code, cancel.
@app.get("/api/claude-auth")
def api_claude_auth():
    return claude_auth.status()


@app.post("/api/claude-auth/login")
def api_claude_auth_login():
    return claude_auth.start_login()


@app.post("/api/claude-auth/code")
def api_claude_auth_code(req: ClaudeLoginCode):
    try:
        return claude_auth.submit_code(req.code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/claude-auth/cancel")
def api_claude_auth_cancel():
    return claude_auth.cancel()


@app.get("/api/cleanup")
def api_cleanup():
    return cleanup.report()


@app.post("/api/cleanup/check")
def api_cleanup_check():
    """Re-check now (no message from Rick, no push)."""
    cleanup.generate()
    return cleanup.report()


@app.post("/api/cleanup/apply")
def api_cleanup_apply(req: CleanupAction):
    try:
        return cleanup.apply(req.id, req.title)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except chat_jobs.AgentBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/cleanup/dismiss")
def api_cleanup_dismiss(req: CleanupAction):
    if not cleanup.dismiss(req.id):
        raise HTTPException(status_code=404, detail="that suggestion is no longer there")
    return {"ok": True}


# ── The manager's proposals (proposals.py) and weekly audit (audit.py) ──
@app.get("/api/proposals")
def api_proposals():
    return {"proposals": proposals.list_open(), "manager": agents_module.manager_id()}


@app.post("/api/proposals/{item_id}/approve")
def api_proposal_approve(item_id: str, req: dict | None = None):
    content = (req or {}).get("content")
    try:
        return proposals.approve(item_id, content if isinstance(content, str) else None)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except chat_jobs.AgentBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/proposals/{item_id}/decline")
def api_proposal_decline(item_id: str, req: dict | None = None):
    note = (req or {}).get("note")
    try:
        proposals.decline(item_id, note if isinstance(note, str) else None)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True}


@app.post("/api/proposals/{item_id}/discuss")
def api_proposal_discuss(item_id: str):
    try:
        where = proposals.discuss(item_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not where["agent_id"]:
        raise HTTPException(status_code=409, detail="there's no manager to discuss it with")
    vault.ensure_conversation(where["agent_id"], where["conversation_id"], "Audit")
    return where


# Weekly review (Control Panel): what the Monday cleanup and the manager's
# audit do. Ticks save through /api/settings {"weekly_review": {key: bool}}.
@app.get("/api/weekly-review")
def api_weekly_review():
    on = service_settings.load()["weekly_review"]
    manager = agents_module.manager_id()
    return {"items": [{"key": k, "label": label, "part": part, "on": on[k]}
                      for k, label, part in service_settings.WEEKLY_REVIEW],
            "manager": AGENTS[manager].name if manager in AGENTS else None, "manager_id": manager,
            "last_week": cleanup._load().get("last_week")}


@app.post("/api/weekly-review/run")
def api_weekly_review_run():
    """Run it now (cleanup, then the audit) instead of waiting for Monday.
    Waits for the cleanup, so the page can show its result straight away; the
    audit is the manager's own chat run and only gets queued here."""
    return {"started": True, **cleanup.run_weekly()}


@app.post("/api/audit/run")
def api_audit_run():
    result = audit.run()
    if not result.get("queued"):
        raise HTTPException(status_code=409, detail=result.get("reason") or "could not start the audit")
    return result


@app.post("/api/cleanup/unarchive")
def api_cleanup_unarchive(req: UnarchiveRequest):
    if req.agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="unknown agent")
    try:
        if not vault.unarchive_conversation(req.agent_id, req.conversation_id):
            raise HTTPException(status_code=404, detail="no such archived conversation")
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@app.get("/api/setup")
def api_setup():
    """What's set up on this computer (setup_check.py): model routes, extras,
    and the first-run steps (manager, ready-made agents to suggest)."""
    return {**setup_check.summary(), **setup_check.first_run()}


@app.post("/api/setup/key")
async def api_setup_key(request: Request):
    """{provider: "anthropic"|"xai", key} -> checked, saved to OPENCLAVE_HOME/.env
    and live without a restart. The body is read by hand so that no error
    response (e.g. FastAPI's validation errors) ever echoes the key back."""
    try:
        req = await request.json()
    except ValueError:
        req = None
    provider, key = (req.get("provider"), req.get("key")) if isinstance(req, dict) else (None, None)
    if not isinstance(provider, str) or not isinstance(key, str):
        raise HTTPException(status_code=400, detail="provider and key are needed")
    from starlette.concurrency import run_in_threadpool
    try:
        return await run_in_threadpool(setup_check.save_key, provider, key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/version")
def api_version():
    # Stale-deploy check (see deploy_state.py) — surfaced as a banner on the
    # dashboard and Control Panel when backend code changed without a restart.
    return {**deploy_state.status(), "manager_problem": agents_module.manager_problem()}


@app.get("/api/model-check")
def api_model_check():
    """Cached new-model report; kicks off a background refresh when over a day old."""
    result = model_discovery.cached()
    refreshing = model_discovery.is_stale(result) and model_discovery.refresh_in_background()
    return {"result": result, "refreshing": refreshing}


@app.post("/api/model-check")
def api_model_check_now():
    return {"result": model_discovery.check(), "refreshing": False}


# Model updates (model_updater.py): automatic adds/hides, reviewed here.
@app.get("/api/model-updates")
def api_model_updates():
    return model_updater.summary()


@app.post("/api/model-updates/run")
def api_model_updates_run():
    return {"started": model_updater.run_in_background()}


@app.post("/api/model-updates/enabled")
def api_model_updates_enabled(req: dict):
    model_updater.set_enabled(bool(req.get("on")))
    return model_updater.summary()


@app.post("/api/model-updates/{action}")
def api_model_updates_review(action: str, req: dict):
    if action not in ("remove", "restore"):
        raise HTTPException(status_code=404, detail="unknown action")
    try:
        getattr(model_updater, action)(str(req.get("id") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return model_updater.summary()


@app.get("/api/credits")
def api_credits():
    return provider_credits.get()


@app.get("/api/quota")
def api_quota():
    """Subscription quota for the Control Panel. Codex: last reading captured
    from an OpenClave call (llm_client._record_codex_rate_limits), else the
    newest interactive Codex session log (same subscription). The Claude CLI
    reports no quota at all."""
    codex = None
    try:
        codex = json.loads(llm_client.CLI_QUOTA_FILE.read_text(encoding="utf-8")).get("codex")
    except (OSError, ValueError):
        pass
    if codex is None:
        sessions = sorted(Path.home().glob(".codex/sessions/*/*/*/*.jsonl"), key=lambda p: p.stat().st_mtime)
        if sessions:
            try:
                found = llm_client.codex_rate_limits_from_log(sessions[-1].read_text(encoding="utf-8"))
            except (OSError, ValueError):
                found = None
            if found:  # observed when that session last wrote, not now
                codex = {**found, "observed_at": datetime.fromtimestamp(sessions[-1].stat().st_mtime, timezone.utc).isoformat()}
    return {"codex": codex, "claude_cli": None}


@app.get("/api/model-response-times")
def api_model_response_times():
    """Average recent reply time per model across all agents, weighted by samples."""
    totals: dict[str, list[int]] = {}
    for agent_id in AGENTS:
        for model_id, t in vault.read_response_time_averages(agent_id).items():
            acc = totals.setdefault(model_id, [0, 0])
            acc[0] += t["average_ms"] * t["samples"]
            acc[1] += t["samples"]
    return {m: {"average_ms": round(total / n), "samples": n} for m, (total, n) in totals.items() if n}


@app.get("/api/models", response_model=list[ModelOption])
def api_models(agent: str | None = None):
    """?agent=<id> tags the models that support that agent's connections."""
    agent_id = agent if agent in AGENTS else None
    return _picker_options(agent_id)


@app.post("/api/agents/{agent_id}/model")
def api_set_agent_model(agent_id: str, req: SetModelRequest):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="agent not found")
    return _set_model(agent_id, req)


@app.post("/api/agents/order")
def api_set_agent_order(req: SetAgentOrderRequest):
    try:
        agent_order.set_order(req.order)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"order": req.order}


@app.get("/")
def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html", headers={"Cache-Control": "no-store"})  # fresh after every deploy, like /static


@app.get("/control/")
def control_panel():
    return FileResponse(STATIC_DIR / "control.html", headers={"Cache-Control": "no-store"})  # fresh after every deploy, like /static


@app.get("/mobile/sw.js")
def mobile_service_worker():
    # Served from /mobile/ (not /static/mobile/) so its scope covers the
    # Home Screen app, which iOS needs before it delivers Web Push.
    return FileResponse(STATIC_DIR / "mobile" / "notification-sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-store"})


@app.get("/mobile")
def mobile_redirect():
    return RedirectResponse("/mobile/", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@app.get("/mobile/")
def mobile_dashboard():
    html = (STATIC_DIR / "mobile" / "index.html").read_text(encoding="utf-8")
    # Every local script/stylesheet gets its cache-buster, so the phone keeps
    # them between opens instead of re-downloading ~200 KB each time.
    for parts in (("mobile", "styles.css"), ("mobile", "app.js"), ("chat_core.js",), ("room_core.js",), ("reply_notifications.js",), ("model_picker.js",), ("first_run.js",),
                  ("vendor", "marked-15.0.12.min.js"), ("vendor", "purify-3.2.6.min.js")):
        path = "/static/" + "/".join(parts)
        html = html.replace(f'{path}"', f'{path}?v={_asset_version(*parts)}"')
    return HTMLResponse(html)


def _agent_shell_html(aid: str) -> str:
    # aid always comes from the AGENTS registry (agents.py), never a request —
    # safe to embed directly, but repr() quotes it correctly regardless.
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenClave</title>
<style>html,body{{margin:0;height:100%;background:#111416}}</style>
</head>
<body>
<script>window.OPENCLAVE_AGENT_ID = {aid!r};</script>
<script src="/static/reply_notifications.js"></script>
<script src="/static/chat_core.js"></script>
<script src="/static/dashboard_shell.js"></script>
</body>
</html>"""


for _agent_id in list(AGENTS):
    _register_agent_routes(_agent_id)


app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")
