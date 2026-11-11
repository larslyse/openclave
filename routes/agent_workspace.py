"""Per-agent API under /<agent_id>/, second half: the agent's phone page, plan, backlog,
kanban, usage, tracking, dashboard source, files tab and model. Added to the router
build_agent_router (routes/agent_api.py) makes, after its own routes."""
import base64
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
import llm_client
import llm_models
import svg_avatar
import vault
import custom_pages
import page_history
from schemas import (
    WorkspaceFileCreate, WorkspaceFileRename, WorkspaceFileWrite, BacklogResponse, KanbanResponse,
    KanbanItem, LogMeasurementRequest, LogMeasurementsRequest, ModelOption, PlanResponse,
    PageAddRequest, SetKanbanStageRequest, SetModelRequest, TrackingResponse, UsageResponse,
    UsageWindow,
)
from routes.library import _avatar_file
from routes.models import _model_option, _picker_options, _set_model


def add_workspace_routes(router: APIRouter, agent_id: str) -> None:
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
