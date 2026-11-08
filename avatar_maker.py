"""Avatar maker: fetch a picture from a URL, and turn a cropped picture into a
cartoon, for the avatar editor in the Control Panel (static/avatar_maker.js).

The browser does the cropping (drag and zoom in a canvas). The server only
fetches remote pictures, because the browser can't read most of them
(cross-origin), and runs the cartoon filter, because Pillow is already here.
No AI model and no extra dependency: the filter is classic image processing
(smooth, reduce to flat colours, draw dark outlines), deterministic and free.
A picture can also be tinted in the agent's colour (tint); the untinted one is
kept next to the avatar so a colour change re-tints it (agent_factory.set_color).

Fetching a URL on the server is a classic way to probe a private network
(SSRF), so fetch_image only talks to public addresses: http(s) only, every
resolved address must be a global one, redirects are followed by hand and
re-checked, and the download is capped in size and must decode as an image.
"""
import io
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import requests
from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError

MAX_DOWNLOAD = 10_000_000      # bytes
MAX_SIDE = 1600                # px; the editor never needs more
MAX_REDIRECTS = 3
TIMEOUT_S = 10
STYLES = ("cartoon", "comic", "poster")


class AvatarError(ValueError):
    """Shown to the user as is."""


def _check_public(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise AvatarError("Use an http:// or https:// image address.")
    if parts.username or parts.password:
        raise AvatarError("Addresses with a username or password aren't supported.")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise AvatarError("That address couldn't be found.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise AvatarError("Only pictures on the public internet can be fetched.")


def fetch_image(url: str) -> Image.Image:
    """Download a picture from a public URL and return it decoded, at most
    MAX_SIDE px on its longer side."""
    url = (url or "").strip()
    session = requests.Session()
    session.trust_env = False  # no proxies from the environment
    for _ in range(MAX_REDIRECTS + 1):
        _check_public(url)
        try:
            response = session.get(url, stream=True, timeout=TIMEOUT_S, allow_redirects=False,
                                   headers={"User-Agent": "OpenClave avatar maker", "Accept": "image/*"})
        except requests.RequestException:
            raise AvatarError("Couldn't download that address.")
        if response.is_redirect:
            location = response.headers.get("location", "")
            response.close()
            url = urljoin(url, location)
            continue
        break
    else:
        raise AvatarError("Too many redirects.")
    with response:
        if response.status_code != 200:
            raise AvatarError(f"The site answered {response.status_code}.")
        if int(response.headers.get("content-length") or 0) > MAX_DOWNLOAD:
            raise AvatarError("That picture is too large (max 10 MB).")
        data = bytearray()
        for chunk in response.iter_content(64 * 1024):
            data += chunk
            if len(data) > MAX_DOWNLOAD:
                raise AvatarError("That picture is too large (max 10 MB).")
    return decode(bytes(data))


def decode(data: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > 40_000_000:
            raise AvatarError("That picture is too large.")
        image.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError):
        raise AvatarError("That address isn't a picture OpenClave can read (try PNG, JPEG, WebP or GIF).")
    image = ImageOps.exif_transpose(image)
    image = image.convert("RGBA") if "A" in image.getbands() or image.mode == "P" else image.convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    return image


def to_png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


# Per style and strength (1, 2, 3): flat colours, outline sensitivity (lower =
# more lines), outline width, and the shortest outline kept (px at WORK size;
# drops the specks texture like grass or hair would leave).
_STYLE = {
    "cartoon": {"colors": (12, 10, 8), "line": (9, 7, 5), "thick": 1, "min_line": 50, "sat": 1.3},
    "comic":   {"colors": (9, 8, 6),   "line": (8, 6, 4), "thick": 2, "min_line": 60, "sat": 1.4},
    "poster":  {"colors": (7, 5, 4),   "line": None,      "thick": 0, "min_line": 0,  "sat": 1.4},
}
WORK = 256  # px the filter runs at; the saved avatar is 256
INK = (22, 20, 24)


def _drop_small(mask: Image.Image, min_size: int) -> Image.Image:
    """Only the connected outline pieces with at least min_size pixels."""
    w, h = mask.size
    px = mask.tobytes()
    seen, out = bytearray(w * h), bytearray(w * h)
    for start in range(w * h):
        if not px[start] or seen[start]:
            continue
        seen[start] = 1
        stack, piece = [start], []
        while stack:
            p = stack.pop()
            piece.append(p)
            x = p % w
            for q in (p - 1 if x else -1, p + 1 if x < w - 1 else -1, p - w, p + w):
                if 0 <= q < w * h and px[q] and not seen[q]:
                    seen[q] = 1
                    stack.append(q)
        if len(piece) >= min_size:
            for p in piece:
                out[p] = 255
    return Image.frombytes("L", (w, h), bytes(out))


def cartoonify(image: Image.Image, style: str = "cartoon", strength: int = 2) -> Image.Image:
    """Flat colours plus dark outlines, Pillow only (~0.15 s). Outlines are a
    difference of Gaussians (dark ridges: eyes, mouth, contours), not raw
    edges, so texture doesn't turn into specks; what specks remain are
    dropped by size. Keeps transparency."""
    if style not in STYLES:
        raise AvatarError(f"Unknown style; use one of {', '.join(STYLES)}.")
    i = max(1, min(3, int(strength))) - 1
    cfg = _STYLE[style]
    alpha = image.getchannel("A") if image.mode == "RGBA" else None
    size = image.size
    work = image.convert("RGB").resize((WORK, WORK), Image.LANCZOS)

    # 1. Smooth texture away, then flat colours.
    smooth = work.filter(ImageFilter.MedianFilter(5)).filter(ImageFilter.MedianFilter(5))
    smooth = ImageEnhance.Color(smooth).enhance(cfg["sat"])
    if style == "poster":
        smooth = ImageEnhance.Contrast(smooth).enhance(1.2)
    flat = smooth.quantize(colors=cfg["colors"][i], method=Image.Quantize.MEDIANCUT,
                           dither=Image.Dither.NONE)
    flat = flat.filter(ImageFilter.ModeFilter(5)).convert("RGB")  # tidy speckles left by quantizing

    # 2. Outlines: where a pixel is darker than its surroundings.
    result = flat.resize(size, Image.LANCZOS)
    if cfg["line"]:
        gray = work.convert("L")
        ridges = ImageChops.subtract(gray.filter(ImageFilter.GaussianBlur(2.0)), gray.filter(ImageFilter.GaussianBlur(1.0)))
        threshold = cfg["line"][i]
        lines = ridges.point(lambda v: 255 if v > threshold else 0)
        lines = ImageOps.expand(ImageOps.crop(lines, 3), 3, fill=0)  # the picture's own border isn't an outline
        lines = _drop_small(lines, cfg["min_line"])
        if cfg["thick"] > 1:
            lines = lines.filter(ImageFilter.MaxFilter(3))
        lines = lines.resize(size, Image.BILINEAR).filter(ImageFilter.GaussianBlur(0.5))
        result = Image.composite(Image.new("RGB", size, INK), result, lines)

    if alpha is not None:
        result = result.convert("RGBA")
        result.putalpha(alpha)
    return result


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def parse_color(color: str) -> tuple:
    color = (color or "").strip().lstrip("#")
    if len(color) != 6 or any(c not in "0123456789abcdefABCDEF" for c in color):
        raise AvatarError("Colour: #RRGGBB.")
    return tuple(int(color[k:k + 2], 16) for k in (0, 2, 4))


def tint(image: Image.Image, color: str) -> Image.Image:
    """The picture in shades of the agent's colour (duotone): shadows near
    black, mid-tones the colour itself, highlights a pale version of it.
    Keeps transparency."""
    rgb = parse_color(color)
    alpha = image.getchannel("A") if image.mode == "RGBA" else None
    gray = ImageOps.autocontrast(image.convert("L"), cutoff=1)
    out = ImageOps.colorize(gray, black=_mix(rgb, (14, 16, 18), 0.82), mid=rgb,
                            white=_mix(rgb, (255, 255, 255), 0.85))
    if alpha is not None:
        out = out.convert("RGBA")
        out.putalpha(alpha)
    return out


def cartoon_png(data: bytes, style: str, strength: int, color: str | None = None) -> bytes:
    """The editor's preview: the style (or "original"), then the tint if a
    colour is given."""
    image = decode(data)
    image.thumbnail((WORK, WORK))
    if style != "original":
        image = cartoonify(image, style, strength)
    if color:
        image = tint(image, color)
    return to_png(image)
