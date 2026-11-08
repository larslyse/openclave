#!/usr/bin/env python3
"""Fold one agent into another; its chats land in one chat folder (folders.py).

    python3 scripts/merge_agent.py <source-id> <target-id> --folder "Name" [--dry-run]

Everything the source agent had ends up inside the target:

- every chat (live and archived, with attachments and compaction history)
  moves to the target, into the folder; the transcript keeps the source's
  name as a prior speaker, so old turns still read right;
- a note <Source>.md in the target holds what the source was for: its
  mission, open backlog items, plan and the feedback its persona collected;
- memory entries move to the target's memory/; tracking sections and
  foundations are appended to the target's own files, labelled with the folder;
- any other files move to <Source>/ in the target.

The source folder is then moved out of the agents folder to
<agents folder>/_Retired/<Folder> (never deleted), so it stops loading on the
next restart. A .tgz of both folders as they were goes to
<state>/merge-backups/ first.

Run it while the service is stopped, or restart the service right after.
The source may not be the manager.
"""
import argparse
import os
import re
import shutil
import sys
import tarfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents  # noqa: E402
import folders  # noqa: E402
import vault  # noqa: E402
from config import CONFIG  # noqa: E402

RETIRED_DIR = "_Retired"
# Files that make up the source agent itself: their content goes into the
# <Source>.md note (or nowhere) rather than being moved as files.
CORE = {
    "agent.json", "mission.md", "persona.md", "plan.md", "backlog.md", "dashboard.html",
    "dashboard.html.bak", "response_times.json", "kanban.json", "tracking.md", "foundations.md",
    "avatar.svg", "avatar.png", "page.json", "page.html", "reminders.json", "tasks.json",
    folders.FOLDERS_FILE,
}
SKIP_DIRS = {"conversations", "memory", ".page-history", "inbox"}


def _body(path: Path) -> str:
    if not path.is_file():
        return ""
    _, body = vault.split_frontmatter(path.read_text(encoding="utf-8"))
    return body.strip()


def _demote(markdown: str, levels: int) -> str:
    out = []
    for line in markdown.splitlines():
        if line.startswith("#"):
            hashes = len(line) - len(line.lstrip("#"))
            line = "#" * min(6, hashes + levels) + line[hashes:]
        out.append(line)
    return "\n".join(out).strip()


def _section(body: str, heading: str) -> str:
    m = re.search(rf"^#+ {re.escape(heading)}[^\n]*\n(.*?)(?=^#+ |\Z)", body, re.M | re.S)
    return m.group(1).strip() if m else ""


def source_note(source_id: str, folder: str, moved_files: list[str]) -> str:
    cfg = agents.AGENTS[source_id]
    root = vault.agent_root(source_id)
    mission, persona = _body(root / "mission.md"), _body(root / "persona.md")
    backlog, plan = _body(root / "backlog.md"), _body(root / "plan.md")
    today = datetime.now().strftime("%Y-%m-%d")
    open_items = [line.strip() for line in backlog.splitlines() if line.strip().startswith("- [ ]")]
    feedback = _section(persona, "Feedback")
    parts = [
        f"# {cfg.name} (retired {today})", "",
        f"{cfg.name} was a separate agent until {today}. Its chats are in the chat folder **{folder}**.", "",
        "## Open items", "",
        "\n".join(open_items) if open_items else "- (none)",
        "", f"## What {cfg.name} covered", "",
        _demote(mission, 1) or "-",
    ]
    if plan:
        parts += ["", f"## {cfg.name}'s plan", "", _demote(plan, 2)]
    if feedback:
        parts += ["", f"## Feedback {cfg.name} had collected", "", feedback]
    if moved_files:
        parts += ["", "## Files", ""] + [f"- `{p}`" for p in moved_files]
    return "\n".join(parts) + "\n"


def _move_conversation_file(src: Path, dst: Path, source_id: str, target_id: str, folder: str, dry: bool) -> None:
    fm, body = vault.split_frontmatter(src.read_text(encoding="utf-8"))
    prior = fm.get("Prior-agents", [])
    if not isinstance(prior, list):
        prior = [str(prior)] if prior else []
    s = agents.AGENTS[source_id]
    for name in (s.name, *s.former_names):
        if name not in prior:
            prior.append(name)
    fm["Prior-agents"] = prior
    fm["Agent"] = agents.AGENTS[target_id].name
    fm["Folder"] = folder
    if dst.exists():
        raise FileExistsError(f"{dst} already exists")
    if dry:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    mtime = src.stat().st_mtime
    dst.write_text(vault.render_frontmatter(fm, body), encoding="utf-8")
    os.utime(dst, (mtime, mtime))  # keep "last active"
    src.unlink()


def move_conversations(source_id: str, target_id: str, folder: str, dry: bool, log) -> int:
    src_dir, dst_dir = vault.conversations_dir(source_id), vault.conversations_dir(target_id)
    count = 0
    for sub in ("", vault.ARCHIVED_DIR):
        for f in sorted((src_dir / sub).glob("*.md") if (src_dir / sub).is_dir() else ()):
            _move_conversation_file(f, dst_dir / sub / f.name, source_id, target_id, folder, dry)
            count += 1
    # Attachments and pre-compaction archives, folder by folder.
    for sub in ("attachments", "archive"):
        d = src_dir / sub
        if not d.is_dir():
            continue
        for item in sorted(d.iterdir()):
            target = dst_dir / sub / item.name
            if target.exists():
                raise FileExistsError(f"{target} already exists")
            if not dry:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item), str(target))
    return count


def move_memory(source_id: str, target_id: str, dry: bool, log) -> int:
    src, dst = vault.memory_dir(source_id), vault.memory_dir(target_id)
    n = 0
    for f in sorted(src.glob("*.md") if src.is_dir() else ()):
        name = f.name if not (dst / f.name).exists() else f"{source_id}-{f.name}"
        log(f"  memory {f.name} -> memory/{name}")
        if not dry:
            dst.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dst / name))
        n += 1
    return n


def append_sections(source_file: Path, target_file: Path, label: str, title: str, dry: bool, log) -> None:
    """tracking.md / foundations.md: the source's ## sections go under the
    target's own, each heading prefixed with the label."""
    body = _body(source_file)
    keep = [s for s in re.split(r"(?m)^(?=## )", body) if s.startswith("## ")]
    if not keep:
        return
    renamed = [re.sub(r"^## ", f"## {label}: ", s, count=1).rstrip() for s in keep]
    log(f"  {source_file.name}: {len(renamed)} section(s) appended to the target's {target_file.name}")
    if dry:
        return
    if target_file.exists():
        text = target_file.read_text(encoding="utf-8").rstrip() + "\n\n" + "\n\n".join(renamed) + "\n"
    else:
        text = f"# {title}\n\n" + "\n\n".join(renamed) + "\n"
    target_file.write_text(text, encoding="utf-8")


def move_other_files(source_id: str, target_id: str, dry: bool, log) -> list[str]:
    src = vault.agent_root(source_id)
    sub = agents.AGENTS[source_id].folder
    dst = vault.agent_root(target_id) / sub
    moved = []
    for f in sorted(src.rglob("*")):
        rel = f.relative_to(src)
        if not f.is_file() or rel.parts[0] in SKIP_DIRS or (len(rel.parts) == 1 and rel.name in CORE):
            continue
        if any(p.startswith(".") for p in rel.parts):
            continue
        target = dst / rel
        if target.exists():
            raise FileExistsError(f"{target} already exists")
        rel_target = (Path(sub) / rel).as_posix()
        log(f"  file {rel.as_posix()} -> {rel_target}")
        moved.append(rel_target)
        if not dry:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(target))
    return moved


def backup(source_id: str, target_id: str) -> Path:
    out_dir = CONFIG.state_path("merge-backups")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{source_id}-into-{target_id}.tgz"
    with tarfile.open(out, "w:gz") as tar:
        for agent_id in (source_id, target_id):
            root = vault.agent_root(agent_id)
            tar.add(root, arcname=root.name)
    return out


def merge(source_id: str, target_id: str, folder: str, *, dry: bool = False, log=print) -> dict:
    if source_id not in agents.AGENTS or target_id not in agents.AGENTS:
        raise SystemExit(f"unknown agent: {source_id if source_id not in agents.AGENTS else target_id}")
    if source_id == target_id:
        raise SystemExit("source and target are the same agent")
    if agents.has_role(source_id, agents.MANAGER):
        raise SystemExit("the source is the manager; give the manager role to another agent first")
    folder = " ".join(folder.split())
    if not folder:
        raise SystemExit("a folder needs a name")
    s, t = agents.AGENTS[source_id], agents.AGENTS[target_id]
    src_root, dst_root = vault.agent_root(source_id), vault.agent_root(target_id)
    note_path = dst_root / f"{s.folder}.md"
    if note_path.exists():
        raise SystemExit(f"{t.name} already has {note_path.name}")
    log(f"{'[dry run] ' if dry else ''}{s.name} -> {t.name}, chat folder {folder!r}")
    if not dry:
        log(f"  backup: {backup(source_id, target_id)}")

    moved_files = move_other_files(source_id, target_id, dry, log)
    note = source_note(source_id, folder, moved_files)
    log(f"  note {note_path.name} ({len(note)} chars)")
    if not dry:
        note_path.write_text(note, encoding="utf-8")
    append_sections(src_root / "tracking.md", dst_root / "tracking.md", folder, f"{t.name} — Tracking", dry, log)
    append_sections(src_root / "foundations.md", dst_root / "foundations.md", folder, f"{t.name} — Foundations", dry, log)
    memories = move_memory(source_id, target_id, dry, log)
    chats = move_conversations(source_id, target_id, folder, dry, log)
    if not dry and folder not in (f["name"] for f in folders.list_folders(target_id)):
        folders.create_folder(target_id, folder)
    log(f"  {chats} chat(s), {memories} memory entr{'y' if memories == 1 else 'ies'}, {len(moved_files)} file(s)")

    retired = src_root.parent / RETIRED_DIR / src_root.name
    if retired.exists():
        retired = retired.with_name(f"{src_root.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    log(f"  retire {src_root.name}/ -> {RETIRED_DIR}/{retired.name}/")
    if not dry:
        retired.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_root), str(retired))
        (retired / "RETIRED.md").write_text(
            f"# {s.name} (retired)\n\nFolded into {t.name} on {datetime.now().strftime('%Y-%m-%d')}: "
            f"chats in the folder **{folder}**, background in `{t.folder}/{note_path.name}`.\n", encoding="utf-8")
        agents.AGENTS.pop(source_id, None)
    return {"folder": folder, "chats": chats, "memories": memories, "files": moved_files, "note": note_path.name}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source")
    parser.add_argument("target")
    parser.add_argument("--folder", required=True, help="chat folder in the target agent")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    merge(args.source, args.target, args.folder, dry=args.dry_run)


if __name__ == "__main__":
    main()
