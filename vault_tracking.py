"""Tracking: date x metric tables of measurements in tracking.md. Part of
vault.py's facade: import vault, not this module."""

import re
from datetime import datetime
from pathlib import Path

import vault
from agents import AGENTS
from vault_paths import render_frontmatter, split_frontmatter, write_text


# ── Tracking (open-ended measurements: pain, ROM, rehab sessions, etc.) ──
#
# Stored in the body as one or more date x metric matrices, each under its own
# '## <Section>' heading (e.g. Progress, General Activities, Rehab Activities,
# Strength) — one file, multiple independent tables, rather than a separate
# file per topic, so the owner has one place to look. Same idea as backlog.md's
# checkbox list either way: a hidden YAML list in frontmatter is invisible in
# Obsidian's normal reading view and can't be corrected by hand, which defeats
# the point of a log the owner actually looks at. Columns within a section are
# discovered from whatever metric names have been logged there (preserving
# existing column order, new ones appended) so new kinds of tracked data don't
# need a schema change — just a consistent name reused call to call, and a
# consistent section name to keep it grouped with its own kind of matrix.
#
# Dates are displayed/stored DD-MM-YYYY (the owner's preference) but every other
# date in/out of this module (API responses, the log tool's `date` param) is
# ISO YYYY-MM-DD — the dashboard's own date sorting/parsing assumes ISO, so
# the DD-MM-YYYY conversion happens only at this file's read/write boundary.

def tracking_file(agent_id: str) -> Path:
    return vault.agent_root(agent_id) / "tracking.md"


_DISPLAY_DATE_FMT = "%d-%m-%Y"
_ISO_DATE_FMT = "%Y-%m-%d"
_MATRIX_DATE_RE = re.compile(r"^\d{2}-\d{2}-\d{4}$")
_TRACKING_INSTRUCTIONS = "Edit values directly in the table below to correct a log — it's read on every dashboard load.\n\n"
DEFAULT_TRACKING_SECTION = "Progress"
_SECTION_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def _split_sections(body: str) -> tuple[str, list[tuple[str, str]]]:
    """Splits on '## ' headings into (text_before_first_heading, [(heading, raw_text_incl_heading), ...])."""
    matches = list(_SECTION_HEADING_RE.finditer(body))
    if not matches:
        return body, []
    top_prefix = body[:matches[0].start()]
    sections = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections.append((m.group(1).strip(), body[m.start():end]))
    return top_prefix, sections


def tracking_sections(agent_id: str) -> list[str]:
    """Return this agent's tracking headings, with a generic first-use default."""
    f = tracking_file(agent_id)
    if f.exists():
        _, body = split_frontmatter(f.read_text(encoding="utf-8"))
        _, sections = _split_sections(body)
        names = [title for title, _ in sections]
        if names:
            return names
    return [DEFAULT_TRACKING_SECTION]


def _to_display_date(iso_or_display: str) -> str:
    try:
        return datetime.strptime(iso_or_display, _ISO_DATE_FMT).strftime(_DISPLAY_DATE_FMT)
    except ValueError:
        return iso_or_display  # already display format (or malformed) — pass through


def _to_iso_date(display_or_iso: str) -> str:
    try:
        return datetime.strptime(display_or_iso, _DISPLAY_DATE_FMT).strftime(_ISO_DATE_FMT)
    except ValueError:
        return display_or_iso


def _format_cell(value: str | None, unit: str | None, note: str | None) -> str:
    # A note-only cell (no value — e.g. a Check-ins narrative, or a session
    # log with no single number) keeps an explicit leading "— " marker rather
    # than stripping it away, so _parse_cell can tell "no value, this is all
    # free text" apart from "a value happens to be a multi-word phrase" on
    # read. Without this, a pure-text note like "Sand dunes, 15kg backpack..."
    # silently mis-parsed as value="Sand", unit="dunes, 15kg backpack..." —
    # found 2026-09-15 checking the existing 'hike' entry, not new here.
    if not value:
        return f"— {note}" if note else ""
    s = value
    if unit:
        s = f"{s} {unit}".strip()
    if note:
        s = f"{s} — {note}".strip(" —")
    return s


def _parse_cell(cell: str) -> tuple[str | None, str | None, str | None]:
    cell = cell.strip()
    if not cell:
        return None, None, None
    if cell.startswith("— "):
        return None, None, cell[2:].strip() or None
    main, _, note = cell.partition(" — ")
    value, _, unit = main.strip().partition(" ")
    return (value or None), (unit.strip() or None), (note.strip() or None)


def _parse_tracking_matrix(body: str) -> tuple[str, list[str], dict[str, dict[str, str]]]:
    """Returns (prefix_text_before_table, columns, {display_date: {metric: cell}})."""
    lines = body.splitlines()
    header_idx = None
    columns: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|") and stripped.strip("|").split("|")[0].strip().lower() == "date":
            columns = [c.strip() for c in stripped.strip("|").split("|")][1:]
            header_idx = i
            break
    if header_idx is None:
        return body, [], {}
    prefix = "\n".join(lines[:header_idx])
    if prefix:
        prefix += "\n"
    rows: dict[str, dict[str, str]] = {}
    for line in lines[header_idx + 1:]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not cells or not _MATRIX_DATE_RE.match(cells[0]):
            continue  # separator row or junk — skip
        row = {col: val for col, val in zip(columns, cells[1:]) if val}
        rows[cells[0]] = row
    return prefix, columns, rows


def _render_tracking_matrix(columns: list[str], rows: dict[str, dict[str, str]]) -> str:
    header = "| Date | " + " | ".join(columns) + " |"
    sep = "|---|" + "|".join(["---"] * len(columns)) + "|"
    sorted_dates = sorted(rows.keys(), key=lambda d: datetime.strptime(d, _DISPLAY_DATE_FMT), reverse=True)
    lines = [header, sep]
    for d in sorted_dates:
        row = rows[d]
        lines.append("| " + d + " | " + " | ".join(row.get(c, "") for c in columns) + " |")
    return "\n".join(lines) + "\n"


def log_measurement(
    agent_id: str,
    metric: str,
    value: str | None,
    unit: str | None,
    note: str | None,
    on_date: str | None,
    section: str = DEFAULT_TRACKING_SECTION,
) -> None:
    log_measurements(agent_id, [{
        "metric": metric, "value": value, "unit": unit, "note": note, "date": on_date,
    }], section=section)


def log_measurements(agent_id: str, new_entries: list[dict], section: str = DEFAULT_TRACKING_SECTION) -> None:
    """Writes any number of entries into one section's date x metric matrix in
    one read-modify-write. Callers that need to log several metrics at once
    (e.g. the dashboard's Log button) MUST batch through here rather than
    firing one call per metric — separate concurrent calls each do their own
    read-modify-write on the same tracking.md with no locking, so parallel
    requests race and silently drop each other's entries."""
    f = tracking_file(agent_id)
    agent_name = AGENTS[agent_id].name
    if f.exists():
        fm, body = split_frontmatter(f.read_text(encoding="utf-8"))
    else:
        fm, body = {}, f"# {agent_name} — Tracking\n\n{_TRACKING_INSTRUCTIONS}"
    top_prefix, sections = _split_sections(body)
    idx = next((i for i, (t, _) in enumerate(sections) if _slug(t) == _slug(section)), None)
    if idx is None:
        sections.append((section, f"## {section}\n\n"))
        idx = len(sections) - 1
    title, section_text = sections[idx]
    prefix, columns, rows = _parse_tracking_matrix(section_text)
    today_iso = datetime.now().strftime(_ISO_DATE_FMT)
    for e in new_entries:
        date_disp = _to_display_date(e.get("date") or today_iso)
        metric = e["metric"]
        if metric not in columns:
            columns.append(metric)
        rows.setdefault(date_disp, {})[metric] = _format_cell(e.get("value"), e.get("unit"), e.get("note"))
    sections[idx] = (title, prefix + _render_tracking_matrix(columns, rows) + "\n")
    body = top_prefix + "".join(text for _, text in sections)
    fm["Updated"] = today_iso
    write_text(agent_id, f, render_frontmatter(fm, body))


def read_measurements(
    agent_id: str, metric: str | None = None, limit: int | None = None, section: str = DEFAULT_TRACKING_SECTION
) -> dict:
    f = tracking_file(agent_id)
    if not f.exists():
        return {"updated": "", "entries": []}
    fm, body = split_frontmatter(f.read_text(encoding="utf-8"))
    _, sections = _split_sections(body)
    idx = next((i for i, (t, _) in enumerate(sections) if _slug(t) == _slug(section)), None)
    if idx is None:
        return {"updated": str(fm.get("Updated", "")), "entries": []}
    _, section_text = sections[idx]
    _, columns, rows = _parse_tracking_matrix(section_text)
    entries = []
    for date_disp, row in rows.items():
        for met, cell in row.items():
            value, unit, note = _parse_cell(cell)
            entries.append({"date": _to_iso_date(date_disp), "metric": met, "value": value, "unit": unit, "note": note})
    if metric:
        entries = [e for e in entries if e.get("metric", "").lower() == metric.lower()]
    entries = sorted(entries, key=lambda e: e.get("date", ""), reverse=True)
    if limit:
        entries = entries[:limit]
    return {"updated": str(fm.get("Updated", "")), "entries": entries}
