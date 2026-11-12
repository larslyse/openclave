"""OpenClave — single FastAPI app serving every agent.

One agent = one entry in agents.AGENTS, not one service. Routes for each agent
are generated from the same router factory (build_agent_router) rather than
hand-written per agent — this is what replaced the reba-service/fresh-service
copy-paste split from 2026-09-04.
"""
import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from config import CONFIG
import agent_factory
import access
from urllib.parse import quote
import migrations
import updater
import model_updater
import tools_doc
import reminders
import scheduled_tasks
import attention
import cleanup
import chat_jobs
import recaps
import scheduler
import voice_engine
from agents import AGENTS
from routes.common import VOICE_ENABLED_AGENTS, STATIC_DIR, _lan_address
from routes.agent_api import build_agent_router
from routes.pages import _agent_shell_html
from routes import library as library_routes
from routes import pages as page_routes
from routes import review as review_routes
from routes import tools as tool_routes
from routes import conversations as conversation_routes
from routes import push as push_routes
from routes import claves as clave_routes
from routes import agents as agent_routes
from routes import models as model_routes
from routes import setup as setup_routes
from routes import jobs as job_routes

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
def start_background_jobs():
    # Each start() registers the feature's job with the one scheduler thread
    # (scheduler.py). With OPENCLAVE_DISABLE_BACKGROUND_JOBS set they are
    # registered switched off, so /api/jobs still lists them; the chat kicker
    # registered above keeps running either way.
    reminders.start()
    scheduled_tasks.start()
    attention.start()
    cleanup.start()  # Weekly maintainer cleanup suggestions.
    recaps.backfill(list(AGENTS))  # once: cards for archived chats that have none yet
    model_updater.start()  # first run 15 min after startup, then daily
    updater.start()  # [updates] check: git fetch at most every 6 hours
    scheduler.start()


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


@app.on_event("shutdown")
def stop_scheduler():
    scheduler.stop()


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


def _register_agent_routes(agent_id: str) -> None:
    """API routes plus the agent's page. Called for every agent at startup and
    for a newly created one straight away, so it works without a restart."""
    VOICE_ENABLED_AGENTS.add(agent_id)
    app.include_router(build_agent_router(agent_id), prefix=f"/{agent_id}")
    app.get(f"/{agent_id}")(lambda: HTMLResponse(_agent_shell_html(agent_id)))


# Route modules (routes/), in the order their routes first appeared in this
# file. FastAPI matches in registration order; tests/test_route_table.py
# checks that every path still reaches the same handler.
app.include_router(library_routes.router)
app.include_router(page_routes.router)
app.include_router(review_routes.router)
app.include_router(tool_routes.router)
app.include_router(conversation_routes.router)
app.include_router(push_routes.router)
app.include_router(clave_routes.router)
app.include_router(agent_routes.router)
app.include_router(model_routes.router)
app.include_router(setup_routes.router)
app.include_router(job_routes.router)


for _agent_id in list(AGENTS):
    _register_agent_routes(_agent_id)


app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")
