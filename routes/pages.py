"""HTML pages: the desktop dashboard, the Control Panel, the mobile app, sign-in, and the shell page each agent gets."""
from fastapi import APIRouter, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
import access
from routes.common import STATIC_DIR

router = APIRouter()


def _asset_version(*relative_parts: str) -> int:
    """mtime-based cache-buster for one static file, in seconds — changes
    exactly when the file does, no content hashing needed at this scale."""
    return int((STATIC_DIR.joinpath(*relative_parts)).stat().st_mtime)


@router.get("/login", response_class=HTMLResponse)
def login_form(next: str = "/"):
    return HTMLResponse(access.login_page(next))


@router.post("/login")
async def login_submit(request: Request, next: str = "/"):
    form = await request.form()
    if not access.token_ok(str(form.get("token") or "")):
        return HTMLResponse(access.login_page(next, "That token isn't right."), status_code=401)
    safe = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(safe, status_code=303)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(**access.cookie_args(secure))
    return response


@router.get("/")
def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html", headers={"Cache-Control": "no-store"})  # fresh after every deploy, like /static


@router.get("/control/")
def control_panel():
    return FileResponse(STATIC_DIR / "control.html", headers={"Cache-Control": "no-store"})  # fresh after every deploy, like /static


@router.get("/mobile/sw.js")
def mobile_service_worker():
    # Served from /mobile/ (not /static/mobile/) so its scope covers the
    # Home Screen app, which iOS needs before it delivers Web Push.
    return FileResponse(STATIC_DIR / "mobile" / "notification-sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-store"})


@router.get("/mobile")
def mobile_redirect():
    return RedirectResponse("/mobile/", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/mobile/")
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
