"""Setup and settings: first-run setup and API keys, version, access, network, phone, updates and the Claude CLI sign-in."""
from fastapi import APIRouter, HTTPException, Request
from config import CONFIG
import deploy_state
from version import VERSION
import phone_setup
import updater
from urllib.parse import urlsplit
import service_settings
import claude_auth
import voice_engine
import setup_check
import agents as agents_module
from schemas import ClaudeLoginCode
from routes.common import _lan_address, _main

router = APIRouter()


@router.get("/api/settings")
def api_get_settings():
    return service_settings.load()


@router.get("/api/access")
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


@router.get("/api/network")
def api_network():
    """Network values needed by the mobile client, without exposing settings."""
    return {
        "secure_mobile_url": f"{CONFIG.public_url}/mobile/" if CONFIG.public_url else "",
        "voice_available": voice_engine.available(),
    }


# Control Panel → Phone (phone_setup.py) and → Update (updater.py).
@router.get("/api/phone")
def api_phone():
    saved = phone_setup.saved_public_url(CONFIG.home / "openclave.toml")
    return {**phone_setup.status(CONFIG.port), "public_url": saved,
            "https": saved.startswith("https://"), "port": CONFIG.port,
            "restart_needed": saved != CONFIG.public_url}


@router.post("/api/phone/setup")
def api_phone_setup():
    result = phone_setup.setup(CONFIG.port, CONFIG.home / "openclave.toml")
    if result["ok"]:
        # Trust the new address at once (the origin check reads this set on
        # every request); other places that show public_url follow on restart.
        public = urlsplit(result["url"])
        _main().TRUSTED_ORIGINS.add(f"{public.scheme}://{public.netloc}")
    return result


@router.get("/api/update")
def api_update():
    return {"current": updater.version(), "check": updater.cached(), "checking": updater.is_checking(),
            "enabled": CONFIG.update_check, "restart": updater.restart_mode(),
            "local_changes": updater.local_changes()}


@router.post("/api/update/check")
def api_update_check():
    return {"started": updater.check_in_background()}


@router.post("/api/update/apply")
def api_update_apply():
    mode = updater.restart_mode()
    if not mode:
        raise HTTPException(status_code=409, detail="OpenClave isn't running as a service that can restart "
                            "itself. Run `openclave update` in a terminal instead.")
    result = updater.apply()
    if result["ok"] and result.get("changed"):
        updater.restart_self(mode)
        result["restarting"] = True
    return result


@router.post("/api/settings")
def api_update_settings(changes: dict):
    try:
        return service_settings.update(changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# Claude CLI sign-in (claude_auth.py): status, start, code, cancel.
@router.get("/api/claude-auth")
def api_claude_auth():
    return claude_auth.status()


@router.post("/api/claude-auth/login")
def api_claude_auth_login():
    return claude_auth.start_login()


@router.post("/api/claude-auth/code")
def api_claude_auth_code(req: ClaudeLoginCode):
    try:
        return claude_auth.submit_code(req.code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/claude-auth/cancel")
def api_claude_auth_cancel():
    return claude_auth.cancel()


@router.get("/api/setup")
def api_setup():
    """What's set up on this computer (setup_check.py): model routes, extras,
    and the first-run steps (manager, ready-made agents to suggest)."""
    return {**setup_check.summary(), **setup_check.first_run()}


@router.post("/api/setup/key")
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


@router.get("/api/version")
def api_version():
    # Stale-deploy check (see deploy_state.py) — surfaced as a banner on the
    # dashboard and Control Panel when backend code changed without a restart.
    return {**deploy_state.status(), "version": VERSION, "manager_problem": agents_module.manager_problem()}
