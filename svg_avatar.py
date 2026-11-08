"""Sanitizing agent-drawn SVG avatars (onboarding's finish_onboarding).

An SVG is a document that can carry scripts, event handlers and external
references, so a model-written one is never stored as-is. This keeps an
allow-list of plain drawing elements and attributes and drops everything
else; the avatar endpoint additionally serves it with a script-blocking CSP.
No rasterizer is installed on this host, so sanitizing (not converting to
PNG) is the containment.
"""
import re
import xml.etree.ElementTree as ET

MAX_BYTES = 20_000
SVG_NS = "http://www.w3.org/2000/svg"

ALLOWED_ELEMENTS = {
    "svg", "g", "circle", "ellipse", "rect", "line", "polyline", "polygon", "path", "text", "tspan",
    "defs", "linearGradient", "radialGradient", "stop", "title",
}
ALLOWED_ATTRS = {
    "viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
    "fx", "fy", "d", "points", "transform", "fill", "stroke", "stroke-width", "stroke-linecap",
    "stroke-linejoin", "opacity", "fill-opacity", "stroke-opacity", "fill-rule", "offset",
    "stop-color", "stop-opacity", "gradientUnits", "gradientTransform", "id", "font-size",
    "font-family", "font-weight", "text-anchor", "dominant-baseline",
}
# url() is only allowed to point at a gradient defined in the same SVG.
_URL_RE = re.compile(r"url\(", re.I)
_LOCAL_URL_RE = re.compile(r"^url\(#[A-Za-z0-9_-]+\)$")


class InvalidSvg(ValueError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def sanitize(svg: str) -> str:
    if len(svg.encode("utf-8")) > MAX_BYTES:
        raise InvalidSvg(f"avatar SVG is larger than {MAX_BYTES // 1000} KB")
    if re.search(r"<!DOCTYPE|<!ENTITY", svg, re.I):
        raise InvalidSvg("avatar SVG may not contain a DOCTYPE or entities")
    try:
        root = ET.fromstring(svg.strip())
    except ET.ParseError as exc:
        raise InvalidSvg(f"avatar SVG is not valid XML: {exc}") from exc
    if _local(root.tag) != "svg":
        raise InvalidSvg("avatar must be an <svg> element")
    _clean(root)
    if "viewBox" not in root.attrib:
        root.set("viewBox", "0 0 64 64")
    ET.register_namespace("", SVG_NS)
    root.tag = f"{{{SVG_NS}}}svg"
    return ET.tostring(root, encoding="unicode")


def _clean(el: ET.Element) -> None:
    el.tag = f"{{{SVG_NS}}}{_local(el.tag)}"
    for name in list(el.attrib):
        value = el.attrib[name]
        local = _local(name)
        keep = local in ALLOWED_ATTRS and "}" not in name  # namespaced attrs (xlink:href etc.) never kept
        if keep and _URL_RE.search(value) and not _LOCAL_URL_RE.match(value.strip()):
            keep = False
        if keep:
            if name != local:
                del el.attrib[name]
                el.set(local, value)
        else:
            del el.attrib[name]
    for child in list(el):
        if _local(child.tag) in ALLOWED_ELEMENTS:
            _clean(child)
        else:
            el.remove(child)
