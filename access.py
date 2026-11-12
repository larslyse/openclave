"""Optional token login for network access.

Off unless [network] access_token (or OPENCLAVE_ACCESS_TOKEN) is set. Then
every request needs the token once: /login asks for it and sets a long-lived
HttpOnly cookie. Plain local requests on this computer (loopback, no proxy
header) don't need it, so local tools and the voice worker keep working; a
request that came through a local reverse proxy (X-Forwarded-For, e.g.
tailscale serve) does need it.
"""
import hashlib
import hmac
import html
from urllib.parse import quote

from config import CONFIG

COOKIE = "openclave_access"
MAX_AGE_S = 365 * 86400
OPEN_PATHS = {"/login", "/favicon.ico"}
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def enabled() -> bool:
    return bool(CONFIG.access_token)


def _cookie_value() -> str:
    # The cookie holds a hash, not the token itself.
    return hashlib.sha256(f"openclave:{CONFIG.access_token}".encode()).hexdigest()


def token_ok(token: str) -> bool:
    return enabled() and hmac.compare_digest((token or "").strip(), CONFIG.access_token)


def allowed(client_host: str | None, headers, cookies, path: str) -> bool:
    if not enabled() or path in OPEN_PATHS:
        return True
    if client_host in _LOOPBACK and not headers.get("x-forwarded-for"):
        return True
    return hmac.compare_digest(cookies.get(COOKIE, ""), _cookie_value())


def wants_html(headers, method: str) -> bool:
    return method == "GET" and "text/html" in (headers.get("accept") or "")


def login_page(next_path: str = "/", error: str = "") -> str:
    safe_next = next_path if next_path.startswith("/") and not next_path.startswith("//") else "/"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenClave sign in</title><style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#121517;color:#e7e4de;font:16px system-ui,sans-serif}}
form{{width:min(340px,90vw);display:flex;flex-direction:column;gap:12px}}
input,button{{font:inherit;padding:10px 12px;border-radius:8px;border:1px solid #30373a;background:#1a1e22;color:inherit}}
button{{background:#e7e4de;color:#121517;font-weight:600;cursor:pointer}} p{{margin:0;color:#e8877a;min-height:1.2em}}
</style></head><body><form method="post" action="/login?next={quote(safe_next)}">
<h1 style="font-size:20px;margin:0">OpenClave</h1>
<input type="password" name="token" placeholder="Access token" autofocus autocomplete="current-password" aria-label="Access token">
<button type="submit">Sign in</button><p role="alert">{html.escape(error)}</p></form></body></html>"""


def cookie_args(secure: bool) -> dict:
    return {"key": COOKIE, "value": _cookie_value(), "max_age": MAX_AGE_S, "httponly": True,
            "samesite": "lax", "secure": secure, "path": "/"}
