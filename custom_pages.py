"""An agent's own mobile page: the button left of the
agent's name opens it.

The agent writes `page.json` in its own folder: a title plus a list of
ready-made blocks that read the agent's own files (Stash's inventory is a
searchable table over catalog.md). Nothing the agent writes runs as code in
the app; the one free-form escape hatch is an `html` block, shown in a
sandboxed frame with no network access. The one block that writes is
`form`: a text box that appends `- [ ] <text>` to one
note the page names; see add_item. Codex agents write page.json with
their own file tools (Codex blocks MCP writes), other routes use the
`write_page` tool; every route can call `check_page` for the format and to
validate what it wrote.
"""
import json
import re
from pathlib import Path

import page_history
import vault

PAGE_FILE = "page.json"
MAX_BLOCKS = 12
MAX_HTML_BYTES = 200 * 1024
MAX_ITEM_CHARS = 300
MAX_FORM_ITEMS_SHOWN = 50
DATA_SUFFIXES = {".md", ".txt"}

FORMAT = """\
Your page: page.json in your own folder. The user opens it with the button left of your name in the phone app.
{
  "title": "Inventory",
  "blocks": [
    {"type": "table", "file": "catalog.md", "title": "Stock", "search": true},
    {"type": "markdown", "file": "notes.md"},
    {"type": "text", "text": "Prices updated each morning."},
    {"type": "form", "file": "shopping.md", "title": "Add to shopping list", "placeholder": "Item", "button": "Add"},
    {"type": "sliders", "title": "Today", "metrics": [{"id": "pain", "label": "Pain", "min": 0, "max": 10, "step": 1}]},
    {"type": "html", "file": "page.html"}
  ]
}
Blocks (up to 12, shown top to bottom):
- table: every markdown table in `file`, grouped under the heading above each table. Optional: "title",
  "search" (search box over all rows), "columns" (only these columns, in this order), "sections" (only the
  tables under these headings, e.g. ["Rehab Activities"]). On a phone each
  row shows as a card: first column bold, the rest below. Keep the data in that file and the page stays current.
- markdown: shows `file` as formatted text.
- text: a short fixed line of text.
- sliders: one slider per metric and a Log button that saves all of them as one dated row in your
  tracking.md (the same log as your tracking tools). Each metric: "id" (the tracking column, lowercase),
  "label", "min", "max", "step", optional "unit". Starts at the latest logged values. Optional: "title",
  "button" (default "Log measurements"), "section" (tracking.md heading, default "Progress").
- form: one text box + button. What the user types is appended to `file` as a new line `- [ ] <text>` (at the
  end of the file), and the box lists that file's `- [ ]`/`- [x]` lines below it. `file` must already
  exist in your folder and can't be a core file (mission, persona, plan, backlog, ...). Optional: "title",
  "placeholder", "button" (default "Add"). Treat those lines as items the user added: tidy them into your
  own format when you next work on that file.
- html: your own HTML page from `file` (.html in your folder), only when no other block fits. Shown in a
  sandboxed frame: inline your CSS/JS, no network, no fetch; put the data in the HTML itself.
Files are paths inside your own folder (.md or .txt, html for the html block). Call check_page after writing
to see what the user will see.
Change your page only when the user asks for it. The user relies on what's there (e.g. sliders they log with): keep
every part they didn't ask to change. Each change is saved as a version, and their phone shows "page changed ·
Keep / Revert" until they decide. If they ask you to go back ("revert your page"), call restore_page, then
tell them which version is back and ask before changing it again."""


class PageError(ValueError):
    pass


def _data_file(agent_id: str, rel_path, suffixes=DATA_SUFFIXES):
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise PageError("needs a \"file\"")
    rel_path = rel_path.strip()
    p = Path(rel_path)
    if p.is_absolute() or any(part in ("", "..") or part.startswith(".") for part in p.parts) or p.parts[0] == "conversations":
        raise PageError(f"{rel_path}: must be a file inside your own folder")
    if p.suffix.lower() not in suffixes:
        raise PageError(f"{rel_path}: must end in " + " or ".join(sorted(suffixes)))
    root = vault.agent_root(agent_id)
    f = (root / p).resolve()
    if root not in f.parents:
        raise PageError(f"{rel_path}: must be a file inside your own folder")
    return f


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


_SEPARATOR = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def parse_tables(text: str) -> list[dict]:
    """Markdown tables, each with the nearest heading above it."""
    sections, heading = [], ""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m:
            heading = m.group(1)
        elif line.lstrip().startswith("|") and i + 1 < len(lines) and _SEPARATOR.match(lines[i + 1].strip()):
            columns = _cells(line)
            rows = []
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = _cells(lines[i])
                cells = (cells + [""] * len(columns))[:len(columns)]
                if any(cells):
                    rows.append(cells)
                i += 1
            sections.append({"heading": heading, "columns": columns, "rows": rows})
            continue
        i += 1
    return sections


def _table(agent_id: str, block: dict) -> dict:
    f = _data_file(agent_id, block.get("file"))
    if not f.is_file():
        raise PageError(f"{block['file']}: file not found")
    sections = parse_tables(f.read_text(encoding="utf-8", errors="replace"))
    if not sections:
        raise PageError(f"{block['file']}: no markdown table found")
    only = block.get("sections")
    if only is not None:
        if not isinstance(only, list) or not all(isinstance(h, str) for h in only):
            raise PageError("\"sections\" must be a list of headings")
        names = {h.strip().lower() for h in only}
        sections = [sec for sec in sections if sec["heading"].strip().lower() in names]
        if not sections:
            raise PageError(f"{block['file']}: no table under " + ", ".join(only))
    wanted = block.get("columns")
    if wanted is not None:
        if not isinstance(wanted, list) or not all(isinstance(c, str) for c in wanted):
            raise PageError("\"columns\" must be a list of column names")
        lower = [c.lower() for c in wanted]
        for s in sections:
            idx = [next((i for i, c in enumerate(s["columns"]) if c.lower() == w), None) for w in lower]
            s["columns"] = [s["columns"][i] if i is not None else w for i, w in zip(idx, wanted)]
            s["rows"] = [[row[i] if i is not None else "" for i in idx] for row in s["rows"]]
    return {"type": "table", "title": str(block.get("title") or ""), "search": bool(block.get("search")),
            "file": block["file"], "sections": sections}


def _resolve(agent_id: str, block) -> dict:
    if not isinstance(block, dict):
        raise PageError("each block must be an object like {\"type\": \"table\", ...}")
    kind = block.get("type")
    if kind == "table":
        return _table(agent_id, block)
    if kind == "markdown":
        f = _data_file(agent_id, block.get("file"))
        if not f.is_file():
            raise PageError(f"{block['file']}: file not found")
        # The note's YAML front matter is metadata, not page content.
        return {"type": "markdown", "title": str(block.get("title") or ""),
                "text": vault.split_frontmatter(f.read_text(encoding="utf-8", errors="replace"))[1]}
    if kind == "text":
        text = block.get("text")
        if not isinstance(text, str) or not text.strip():
            raise PageError("a text block needs \"text\"")
        return {"type": "text", "text": text}
    if kind == "html":
        f = _data_file(agent_id, block.get("file"), {".html"})
        if not f.is_file():
            raise PageError(f"{block['file']}: file not found")
        if f.stat().st_size > MAX_HTML_BYTES:
            raise PageError(f"{block['file']}: larger than {MAX_HTML_BYTES // 1024} KB")
        rel = f.relative_to(vault.agent_root(agent_id)).as_posix()
        return {"type": "html", "title": str(block.get("title") or ""), "file": rel,
                "height": max(120, min(int(block.get("height") or 600), 3000))}
    if kind == "sliders":
        return _sliders(block)
    if kind == "form":
        f = _form_file(agent_id, block)
        return {"type": "form", "title": str(block.get("title") or ""), "file": block["file"],
                "placeholder": str(block.get("placeholder") or "")[:80], "button": str(block.get("button") or "Add")[:24],
                "items": list_items(f.read_text(encoding="utf-8", errors="replace"))}
    raise PageError(f"unknown block type {kind!r}: use table, markdown, text, sliders, form or html")


_METRIC_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def _sliders(block: dict) -> dict:
    metrics = block.get("metrics")
    if not isinstance(metrics, list) or not 1 <= len(metrics) <= 12:
        raise PageError("a sliders block needs \"metrics\": a list of 1 to 12")
    out = []
    for m in metrics:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not _METRIC_ID.match(m["id"]):
            raise PageError("each metric needs an \"id\" like \"pain\" or \"lower_back\" (lowercase, _)")
        try:
            lo, hi, step = float(m.get("min", 0)), float(m.get("max", 10)), float(m.get("step", 1))
        except (TypeError, ValueError):
            raise PageError(f"{m['id']}: min, max and step must be numbers")
        if not lo < hi or step <= 0:
            raise PageError(f"{m['id']}: needs min < max and step > 0")
        unit = m.get("unit")
        out.append({"id": m["id"], "label": str(m.get("label") or m["id"].replace("_", " "))[:40],
                    "min": lo, "max": hi, "step": step, "unit": str(unit)[:12] if unit else None})
    return {"type": "sliders", "title": str(block.get("title") or ""), "metrics": out,
            "button": str(block.get("button") or "Log measurements")[:30],
            "section": str(block.get("section") or "Progress")[:60]}


def _form_file(agent_id: str, block: dict) -> Path:
    f = _data_file(agent_id, block.get("file"), {".md", ".txt"})
    rel = f.relative_to(vault.agent_root(agent_id)).as_posix()
    if vault.is_protected_workspace_file(rel) or rel == PAGE_FILE:
        raise PageError(f"{rel}: a core file can't take items from a form")
    if not f.is_file():
        raise PageError(f"{block['file']}: file not found (create it first)")
    return f


_ITEM = re.compile(r"^\s*[-*] \[( |x|X)\] (.+?)\s*$")


def list_items(text: str) -> list[dict]:
    """The file's checklist lines, newest last (at most MAX_FORM_ITEMS_SHOWN)."""
    items = [{"text": m.group(2), "done": m.group(1) != " "} for m in map(_ITEM.match, text.splitlines()) if m]
    return items[-MAX_FORM_ITEMS_SHOWN:]


def clean_item(text) -> str:
    """One line of plain text, or PageError. Newlines and control characters
    become spaces, so an item can't break out into the rest of the note."""
    if not isinstance(text, str):
        raise PageError("type something first")
    text = re.sub(r"[\x00-\x1f\x7f\u2028\u2029]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise PageError("type something first")
    if len(text) > MAX_ITEM_CHARS:
        raise PageError(f"too long: keep it under {MAX_ITEM_CHARS} characters")
    return text


def add_item(agent_id: str, block_index, text) -> dict:
    """The form block's Add: append `- [ ] <text>` to the note named by form
    block `block_index` of the agent's saved page.json. The page itself is
    the allowlist: the browser sends only the block number, never a path,
    and the path is checked like any page file (inside the agent's folder,
    .md/.txt, not a core file, already exists)."""
    missing = PageError("this page has no such form; reload it")
    if not isinstance(block_index, int) or isinstance(block_index, bool) or not 0 <= block_index < MAX_BLOCKS:
        raise missing
    try:
        spec = json.loads((vault.agent_root(agent_id) / PAGE_FILE).read_text(encoding="utf-8"))
        block = spec["blocks"][block_index]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        raise missing
    if not isinstance(block, dict) or block.get("type") != "form":
        raise missing
    item = clean_item(text)
    target = _form_file(agent_id, block)
    with vault.file_lock(agent_id, target) as scoped:
        current = scoped.read_text(encoding="utf-8") if scoped.exists() else ""
        lead = "" if not current or current.endswith("\n") else "\n"
        # A blank line first unless the file already ends in a list, so the
        # item doesn't run into a table or paragraph above it.
        last = current.rstrip("\n").splitlines()[-1] if current.strip() else ""
        if current.strip() and not _ITEM.match(last):
            lead += "\n"
        vault._write_text_unlocked(scoped, current + lead + f"- [ ] {item}\n")
        updated = scoped.read_text(encoding="utf-8")
    return {"added": item, "items": list_items(updated)}


def load(agent_id: str, spec: dict | None = None) -> dict | None:
    """The page ready to draw, or None if the agent has none. A broken block
    becomes an error block, so one bad file doesn't blank the whole page."""
    if spec is None:
        f = vault.agent_root(agent_id) / PAGE_FILE
        if not f.is_file():
            return None
        try:
            spec = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {"title": "", "blocks": [{"type": "error", "message": f"{PAGE_FILE} is not valid JSON: {exc}"}]}
    if not isinstance(spec, dict) or not isinstance(spec.get("blocks"), list):
        return {"title": "", "blocks": [{"type": "error", "message": f"{PAGE_FILE} needs {{\"title\": ..., \"blocks\": [...]}}"}]}
    blocks = []
    for n, block in enumerate(spec["blocks"][:MAX_BLOCKS], 1):
        try:
            blocks.append(_resolve(agent_id, block))
        except PageError as exc:
            blocks.append({"type": "error", "message": f"block {n}: {exc}"})
    if len(spec["blocks"]) > MAX_BLOCKS:
        blocks.append({"type": "error", "message": f"only the first {MAX_BLOCKS} blocks are shown"})
    return {"title": str(spec.get("title") or ""), "blocks": blocks}


def check(agent_id: str) -> str:
    """For the agent: the format, then what the user will see (or what's wrong)."""
    page_history.safe_snapshot(agent_id)
    history = page_history.describe(agent_id)
    history = f"\n{history}" if history else ""
    page = load(agent_id)
    if page is None:
        return f"You have no page yet (The user sees the app's built-in page, if it has one for you).{history}\n\n{FORMAT}"
    lines = [f"Page \"{page['title'] or '(no title)'}\":"]
    for b in page["blocks"]:
        if b["type"] == "error":
            lines.append(f"- ERROR {b['message']}")
        elif b["type"] == "table":
            rows = sum(len(s["rows"]) for s in b["sections"])
            heads = ", ".join(f"{s['heading'] or '(no heading)'} ({len(s['rows'])})" for s in b["sections"])
            lines.append(f"- table from {b['file']}: {rows} rows in {len(b['sections'])} sections: {heads}"
                         + (" — searchable" if b["search"] else ""))
        elif b["type"] == "markdown":
            lines.append(f"- markdown, {len(b['text'])} characters")
        elif b["type"] == "text":
            lines.append(f"- text: {b['text'][:80]}")
        elif b["type"] == "sliders":
            lines.append(f"- sliders logging {', '.join(m['id'] for m in b['metrics'])} to tracking.md ({b['section']})")
        elif b["type"] == "form":
            lines.append(f"- form adding `- [ ] ...` lines to {b['file']} ({len(b['items'])} there now)")
        else:
            lines.append(f"- html from {b['file']}")
    ok = not any(b["type"] == "error" for b in page["blocks"])
    lines.append("All good: this is what the user sees." if ok else f"Fix the errors above.\n\n{FORMAT}")
    return "\n".join(lines) + history


def write(agent_id: str, page: dict, html: str | None = None) -> str:
    """write_page tool (non-Codex routes): validate, then save page.json
    (and page.html when given)."""
    if not isinstance(page, dict):
        raise PageError("page must be an object: {\"title\": ..., \"blocks\": [...]}")
    root = vault.agent_root(agent_id)
    page_history.safe_snapshot(agent_id)  # the page as it was, so the owner can go back to it
    if html is not None:
        if len(html.encode("utf-8")) > MAX_HTML_BYTES:
            raise PageError(f"html is larger than {MAX_HTML_BYTES // 1024} KB")
        vault.write_text(agent_id, root / "page.html", html)
    result = load(agent_id, page)
    errors = [b["message"] for b in result["blocks"] if b["type"] == "error"]
    if errors:
        raise PageError("not saved: " + "; ".join(errors))
    vault.write_text(agent_id, root / PAGE_FILE, json.dumps(page, indent=2, ensure_ascii=False) + "\n")
    return check(agent_id)  # check() records the new version


def html_file(agent_id: str, rel_path: str):
    """The html block's file, only if page.json really uses it."""
    page = load(agent_id)
    if page is None or not any(b["type"] == "html" and b["file"] == rel_path for b in page["blocks"]):
        return None
    return _data_file(agent_id, rel_path, {".html"})
