"""save_image: an agent saves a product photo from the web into the vault,
next to a note it may write (vault_access folders), and gets the Obsidian embed.
The note doesn't have to exist yet: save the images, then write the note with
the embeds in place (write_vault_note can only create or append).

The URL can be the image itself or a product page; for a page the main image
is taken from its og:image, twitter:image or structured product data. Only
public http(s) hosts (checked on every redirect), raster images checked by
their first bytes (no SVG), size caps. Images land in the nearest existing
"_assets" folder at or above the note, inside the agent's folders; no folder
is ever created. Sites with bot protection are reported, not worked around.
"""
import hashlib
import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx

import vault
import vault_access
from vault_paths import _write_bytes_unlocked

ASSETS = "_assets"
MAX_PAGE_BYTES = 4_000_000
MAX_IMAGE_BYTES = 8_000_000
MIN_IMAGE_BYTES = 1_000
MAX_REDIRECTS = 5
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/130.0 Safari/537.36"}
IMAGE_TYPES = {  # extension -> check on the first bytes
    ".jpg": lambda b: b[:3] == b"\xff\xd8\xff",
    ".png": lambda b: b[:8] == b"\x89PNG\r\n\x1a\n",
    ".gif": lambda b: b[:6] in (b"GIF87a", b"GIF89a"),
    ".webp": lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP",
    ".avif": lambda b: b[4:12] in (b"ftypavif", b"ftypavis"),
}


class ImageError(ValueError):
    pass


def _check_host(url: str) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ImageError("only http(s) links")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except socket.gaierror:
        raise ImageError(f"can't find the site {u.hostname}")
    if any(not ipaddress.ip_address(i[4][0]).is_global for i in infos):
        raise ImageError("links to local or private networks aren't allowed")


def _blocked(r: httpx.Response, body: bytes) -> bool:
    if r.headers.get("cf-mitigated") == "challenge":
        return True
    head = body[:20000].lower()
    return r.status_code in (403, 429, 503) and any(
        s in head for s in (b"just a moment", b"captcha", b"access denied", b"cf-chl", b"datadome", b"perimeterx"))


def _get(url: str, max_bytes: int) -> tuple[httpx.Response, bytes, str]:
    with httpx.Client(timeout=20, headers=HEADERS, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _check_host(url)
            with client.stream("GET", url) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise ImageError(f"larger than {max_bytes // 1_000_000} MB")
                if _blocked(r, body):
                    raise ImageError(f"{urlparse(url).hostname} blocks automated access. Don't retry it; "
                                     "try the manufacturer's page or another shop")
                if r.status_code >= 400:
                    raise ImageError(f"HTTP {r.status_code} from {urlparse(url).hostname}")
                return r, body, url
    raise ImageError("too many redirects")


class _Meta(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.ld, self._ld = {}, [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
        elif tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._ld = []

    def handle_endtag(self, tag):
        if tag == "script" and self._ld is not None:
            self.ld.append("".join(self._ld))
            self._ld = None

    def handle_data(self, data):
        if self._ld is not None:
            self._ld.append(data)


def _walk(node):
    if isinstance(node, list):
        for x in node:
            yield from _walk(x)
    elif isinstance(node, dict):
        yield node
        if "@graph" in node:
            yield from _walk(node["@graph"])


def page_image(html: str, base: str) -> str | None:
    """The page's main image: og:image, twitter:image, then structured product data."""
    p = _Meta()
    p.feed(html)
    for key in ("og:image", "og:image:secure_url", "twitter:image"):
        if p.meta.get(key):
            return urljoin(base, p.meta[key].strip())
    for raw in p.ld:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for n in _walk(data):
            types = n.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" in types and n.get("image"):
                img = n["image"][0] if isinstance(n["image"], list) else n["image"]
                img = img.get("url") if isinstance(img, dict) else img
                if isinstance(img, str) and img.strip():
                    return urljoin(base, img.strip())
    return None


def _assets_dir(note: Path, allowed_roots: list[Path]) -> Path:
    root = next(r for r in allowed_roots if note.is_relative_to(r))
    d = note.parent
    while True:
        if (d / ASSETS).is_dir():
            return d / ASSETS
        if d == root:
            break
        d = d.parent
    raise ImageError(f"there's no '{ASSETS}' folder at or above that note in your vault folders, and folders "
                     "are never created. The owner can add one")


def _slug(text: str) -> str:
    s = re.sub(r"[^\w\- ]+", "", unquote(text), flags=re.UNICODE).strip().replace(" ", "-").lower()
    return re.sub(r"-{2,}", "-", s)[:60].strip("-") or "image"


def save(agent_id: str, url: str, note_path: str, name: str = "") -> dict:
    allowed = vault_access.folders(agent_id)
    if not allowed:
        raise ImageError("you have no vault folders to write in. The owner can add some in Control Panel → Agents → Edit")
    try:
        note = vault.main_vault_target(note_path)
    except vault.MainVaultError as exc:
        raise ImageError(str(exc)) from exc
    vault_root = vault.VAULT_ROOT.resolve()
    roots = [(vault_root / a).resolve() for a in allowed]
    if not any(note.is_relative_to(r) for r in roots):
        raise ImageError(f"{note_path} is outside your vault folders ({', '.join(allowed)})")
    if not note.parent.is_dir():
        raise ImageError("that note's folder doesn't exist (folders are never created)")
    dest = _assets_dir(note, roots)

    r, body, final = _get(str(url or "").strip(), MAX_PAGE_BYTES)
    if "html" in r.headers.get("content-type", "").lower():
        img = page_image(body.decode(r.encoding or "utf-8", errors="replace"), final)
        if not img:
            raise ImageError("that page names no main image (no og:image or product image); pass the image's own link")
        page = final
        r, body, final = _get(img, MAX_IMAGE_BYTES)
    else:
        page = None
    ext = next((e for e, ok in IMAGE_TYPES.items() if ok(body)), None)
    if not ext:
        raise ImageError(f"not a supported image ({r.headers.get('content-type', 'unknown type')}); "
                         "jpg, png, webp, gif or avif only")
    if len(body) < MIN_IMAGE_BYTES:
        raise ImageError("that image is tiny, probably an icon or tracking pixel")

    base = _slug(name) if str(name or "").strip() else _slug(f"{note.stem} {Path(urlparse(final).path).stem}")
    digest = hashlib.sha256(body).hexdigest()
    for n in range(1, 100):
        target = dest / f"{base}{'' if n == 1 else f'-{n}'}{ext}"
        if not target.exists():
            break
        if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            return {"path": str(target.relative_to(vault_root)), "embed": f"![[{target.name}]]",
                    "source": page or final, "new": False}
    else:
        raise ImageError("too many images with that name; give a different name")
    _write_bytes_unlocked(target, body)
    target.chmod(0o664)
    return {"path": str(target.relative_to(vault_root)), "embed": f"![[{target.name}]]",
            "source": page or final, "new": True}


def tool_reply(agent_id: str, tool_input: dict) -> str:
    try:
        out = save(agent_id, tool_input.get("url", ""), tool_input.get("note_path", ""), tool_input.get("name", ""))
    except (ImageError, httpx.HTTPError) as exc:
        return f"refused: {exc}"
    return (f"Success! {'Saved' if out['new'] else 'Already saved'} {out['path']} (from {out['source']}). "
            f"Put {out['embed']} in the note with write_vault_note, with the source on the next line, "
            f"e.g. *Image: <site> product page*.")
