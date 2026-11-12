"""Version history for an agent's pages.

Two kinds of page:
- "phone": page.json plus the html files its html blocks use (custom_pages.py).
  No page.json means the app's built-in page for that agent.
- "desktop": dashboard.html, the agent page on desktop.

Agents change these files with their own tools (Codex writes them natively),
so versions are recorded by looking, not by intercepting writes: snapshot()
runs before and after every chat turn, whenever the phone page loads, and
around write_page / write_own_dashboard. A changed phone page counts as
"waiting for the user" until they tap Keep (or Revert) on their phone, so no agent
replaces a page they use without them seeing it. "Revert your page" works by
the agent calling restore_page.

Stored in the agent's own folder under .page-history/ (hidden from the
Files tab and from page blocks), backed up with the vault.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import vault

HISTORY_DIR = ".page-history"
KINDS = ("phone", "desktop")
MAX_VERSIONS = 30
NONE_HASH = "none"  # no page.json: the built-in page


def _root(agent_id: str) -> Path:
    return vault.agent_root(agent_id) / HISTORY_DIR


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _phone_files(agent_id: str) -> dict[str, str]:
    root = vault.agent_root(agent_id)
    f = root / "page.json"
    if not f.is_file():
        return {}
    text = f.read_text(encoding="utf-8", errors="replace")
    files = {"page.json": text}
    try:
        blocks = json.loads(text).get("blocks") or []
    except (ValueError, AttributeError):
        blocks = []
    for b in blocks if isinstance(blocks, list) else []:
        rel = b.get("file") if isinstance(b, dict) and b.get("type") == "html" else None
        if not isinstance(rel, str):
            continue
        p = Path(rel.strip())
        if p.is_absolute() or any(part in ("", "..") or part.startswith(".") for part in p.parts):
            continue
        html = (root / p).resolve()
        if root.resolve() in html.parents and html.is_file() and html.stat().st_size <= 512 * 1024:
            files[p.as_posix()] = html.read_text(encoding="utf-8", errors="replace")
    return files


def _desktop_files(agent_id: str) -> dict[str, str]:
    f = vault.agent_root(agent_id) / "dashboard.html"
    return {"dashboard.html": f.read_text(encoding="utf-8", errors="replace")} if f.is_file() else {}


def current_files(agent_id: str, kind: str) -> dict[str, str]:
    return _phone_files(agent_id) if kind == "phone" else _desktop_files(agent_id)


def _hash(files: dict[str, str]) -> str:
    if not files:
        return NONE_HASH
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:16]


def _read_index(agent_id: str) -> dict:
    try:
        index = json.loads((_root(agent_id) / "index.json").read_text(encoding="utf-8"))
        return {k: index.get(k, []) for k in KINDS}
    except (OSError, ValueError, AttributeError):
        return {k: [] for k in KINDS}


def _write_index(agent_id: str, index: dict) -> None:
    root = _root(agent_id)
    root.mkdir(parents=True, exist_ok=True)
    vault.write_text(agent_id, root / "index.json", json.dumps(index, indent=1) + "\n")


def _version_dir(agent_id: str, kind: str, v: int) -> Path:
    return _root(agent_id) / kind / f"v{v}"


def _record(agent_id: str, index: dict, kind: str, files: dict[str, str], kept: bool, note: str = "") -> dict:
    entries = index[kind]
    v = (entries[-1]["v"] + 1) if entries else 1
    d = _version_dir(agent_id, kind, v)
    d.mkdir(parents=True, exist_ok=True)
    (d / "files.json").write_text(json.dumps(files, ensure_ascii=False), encoding="utf-8")
    entry = {"v": v, "hash": _hash(files), "at": _now(), "kept": kept, "note": note}
    entries.append(entry)
    # Trim the oldest, but never the newest version the owner kept.
    last_kept = max((e["v"] for e in entries if e["kept"]), default=None)
    while len(entries) > MAX_VERSIONS:
        drop = next(e for e in entries if e["v"] != last_kept)
        entries.remove(drop)
        old = _version_dir(agent_id, kind, drop["v"]) / "files.json"
        old.unlink(missing_ok=True)
        try:
            old.parent.rmdir()
        except OSError:
            pass
    return entry


def snapshot(agent_id: str, kinds=KINDS) -> None:
    """Record each page as a new version if it changed since the last one.
    The first version of each kind is the starting point and counts as kept;
    later phone versions wait for the user, desktop ones don't (no Keep there)."""
    index = _read_index(agent_id)
    changed = False
    for kind in kinds:
        files = current_files(agent_id, kind)
        entries = index[kind]
        if entries and entries[-1]["hash"] == _hash(files):
            continue
        if not entries and not files:
            continue  # nothing to remember yet: built-in page, never changed
        _record(agent_id, index, kind, files, kept=not entries or kind == "desktop")
        changed = True
    if changed:
        _write_index(agent_id, index)


def safe_snapshot(agent_id: str) -> None:
    """For hooks (chat turns): history must never break a reply."""
    try:
        snapshot(agent_id)
    except Exception:  # noqa: BLE001
        pass


def versions(agent_id: str, kind: str = "phone") -> list[dict]:
    return list(_read_index(agent_id)[kind])


def pending(agent_id: str) -> dict | None:
    """The phone page changed since the user last kept one: {"v", "at", "kept_v"}."""
    entries = versions(agent_id, "phone")
    if not entries or entries[-1]["kept"]:
        return None
    kept = [e for e in entries if e["kept"]]
    if not kept:
        return None
    return {"v": entries[-1]["v"], "at": entries[-1]["at"], "kept_v": kept[-1]["v"]}


def keep(agent_id: str) -> None:
    index = _read_index(agent_id)
    if index["phone"] and not index["phone"][-1]["kept"]:
        index["phone"][-1]["kept"] = True
        _write_index(agent_id, index)


def restore(agent_id: str, kind: str = "phone", v: int | None = None, note: str = "") -> dict:
    """Put version v back (default: for the phone, the last one the user kept
    before the current one; for desktop, the one before the current one).
    The restored page is recorded as a new, kept version."""
    if kind not in KINDS:
        raise ValueError("kind must be phone or desktop")
    snapshot(agent_id, (kind,))
    index = _read_index(agent_id)
    entries = index[kind]
    if not entries:
        raise ValueError(f"no {kind} page versions yet")
    if v is None:
        earlier = entries[:-1]
        if kind == "phone":
            earlier = [e for e in earlier if e["kept"]] or earlier
        if not earlier:
            raise ValueError("there is no earlier version")
        target = earlier[-1]
    else:
        target = next((e for e in entries if e["v"] == v), None)
        if target is None:
            raise ValueError(f"no version v{v}; versions: " + ", ".join(f"v{e['v']}" for e in entries))
    try:
        files = json.loads((_version_dir(agent_id, kind, target["v"]) / "files.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError(f"v{target['v']} can't be read")
    root = vault.agent_root(agent_id)
    current = current_files(agent_id, kind)
    for name, text in files.items():
        vault.write_text(agent_id, root / name, text)
    if kind == "phone" and not files and "page.json" in current:
        (root / "page.json").unlink()  # back to the built-in page
    if entries[-1]["hash"] != target["hash"]:
        _record(agent_id, index, kind, files, kept=True, note=note or f"restored v{target['v']}")
    else:
        entries[-1]["kept"] = True
    _write_index(agent_id, index)
    return {"restored": target["v"], "files": sorted(files) or ["(built-in page)"]}


def describe(agent_id: str) -> str:
    """Version list for the agent (check_page)."""
    lines = []
    for kind, label in (("phone", "Phone page"), ("desktop", "Desktop page (dashboard.html)")):
        entries = versions(agent_id, kind)[-8:]
        if not entries:
            continue
        parts = []
        for e in reversed(entries):
            state = "now" if e is entries[-1] else e["at"][:16].replace("T", " ")
            flag = "" if kind == "desktop" else (", the user kept it" if e["kept"] else ", waiting for the user")
            empty = " (built-in page)" if e["hash"] == NONE_HASH else ""
            parts.append(f"v{e['v']} ({state}{flag}{empty}{', ' + e['note'] if e.get('note') else ''})")
        lines.append(f"{label} versions, newest first: " + "; ".join(parts))
    if lines:
        lines.append("Change the page only when the user asks. To go back to a version: restore_page (kind, version).")
    return "\n".join(lines)
