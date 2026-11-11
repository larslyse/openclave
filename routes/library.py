"""Agent pictures (uploads, the avatar library, the avatar maker) and the catalog of ready-made agents."""
import base64
import os
import tempfile
import io
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from config import CONFIG
import agent_factory
import avatar_maker
import catalog
import svg_avatar
import agents as agents_module
from agents import AGENTS
from schemas import AvatarFetchRequest
from routes.common import _join_clave, _register_agent_routes

router = APIRouter()


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


@router.get("/api/agents/{agent_id}/avatar")
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


@router.post("/api/agents/{agent_id}/avatar")
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


@router.get("/api/avatar-library")
def api_avatar_library():
    return {"avatars": catalog.avatar_library()}


@router.get("/api/avatar-library/{avatar_id}.svg")
def api_avatar_library_svg(avatar_id: str):
    try:
        svg = svg_avatar.sanitize(catalog.library_avatar(avatar_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(svg, media_type="image/svg+xml", headers={**_SVG_HEADERS, "Cache-Control": "public, max-age=3600"})


@router.post("/api/agents/{agent_id}/avatar/library")
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
@router.post("/api/avatar/fetch")
def avatar_fetch(req: AvatarFetchRequest):
    """A picture from a public URL, as a PNG data URL the editor can draw."""
    try:
        image = avatar_maker.fetch_image(req.url)
    except avatar_maker.AvatarError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"data_url": "data:image/png;base64," + base64.b64encode(avatar_maker.to_png(image)).decode(),
            "width": image.width, "height": image.height}


@router.post("/api/avatar/cartoon")
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


# ── Ready-made agents (catalog.py) ──
@router.get("/api/catalog")
def api_catalog():
    return {"templates": catalog.listing()}


@router.get("/api/catalog/{template_id}/avatar")
def api_catalog_avatar(template_id: str):
    try:
        t = catalog.get(template_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not t["has_avatar"]:
        raise HTTPException(status_code=404, detail="no avatar")
    svg = svg_avatar.sanitize((t["folder"] / "avatar.svg").read_text(encoding="utf-8"))
    return Response(svg, media_type="image/svg+xml", headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})


@router.post("/api/catalog/{template_id}/install")
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
