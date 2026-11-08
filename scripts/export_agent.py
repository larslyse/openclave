"""Draft a catalog template from one of your agents (catalog/README.md).

    python3 scripts/export_agent.py <agent folder> catalog/agents/<id> [--name NAME]

Copies the mission and persona bodies, the avatar and the agent's own skills,
writes agent.json with a catalog block to fill in and an onboarding.md
skeleton, and leaves out everything else (conversations, memory, logs,
notes). It can't know what's personal, so it lists lines worth checking:
the owner's name from the config, your private list (scripts/check_private.py)
and words that usually are. Rewrite those before committing.
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

KEEP = ("roles", "connections", "mcp_servers", "cli_tools")
HINTS = re.compile(r"\b(my|our|wife|husband|partner|kids?|son|daughter|years old|lives? in|address|born)\b|\d{4}", re.IGNORECASE)


def body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        text = text[end + 4:] if end != -1 else text
    return text.strip() + "\n"


def personal_lines(folder: Path, owner: str) -> list[str]:
    import check_private
    patterns = check_private.load_patterns(check_private.list_path()) if check_private.list_path().is_file() else []
    out = []
    for f in sorted(folder.rglob("*")):
        if not f.is_file() or f.suffix not in (".md", ".json"):
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            why = check_private.find(line, patterns)
            if owner and re.search(rf"\b{re.escape(owner)}\b", line):
                why.append(f"owner name '{owner}'")
            if not why and HINTS.search(line):
                why.append("might be personal")
            if why:
                out.append(f"{f.relative_to(folder)}:{n}: {', '.join(why)}: {line.strip()[:100]}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Draft a catalog template from one of your agents.")
    ap.add_argument("agent_folder", type=Path)
    ap.add_argument("dest", type=Path)
    ap.add_argument("--name", help="the template's name (default: the agent's)")
    args = ap.parse_args(argv)
    src, dest = args.agent_folder.expanduser(), args.dest
    data = json.loads((src / "agent.json").read_text(encoding="utf-8"))
    if dest.exists():
        ap.error(f"{dest} exists; remove it first")
    dest.mkdir(parents=True)
    name = args.name or data["name"]
    out = {"id": re.sub(r"[^a-z0-9]", "", name.lower()), "name": name, "color": data.get("color", "#888888"),
           "tagline": data.get("tagline", ""), **{k: data[k] for k in KEEP if k in data},
           "catalog": {"version": 1, "category": "Other", "summary": "TODO: one or two sentences for the list.",
                       "suggested_tools": None, "authors": []}}
    (dest / "agent.json").write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for f in ("mission.md", "persona.md"):
        (dest / f).write_text(body(src / f), encoding="utf-8")
    (dest / "onboarding.md").write_text("# Onboarding questions\n\nAsk these, one at a time, to fit the template to "
                                        "this user. Skip any the user already answered.\n\n- TODO\n", encoding="utf-8")
    if (src / "avatar.svg").is_file():
        shutil.copyfile(src / "avatar.svg", dest / "avatar.svg")
    if (src / "skills").is_dir():
        shutil.copytree(src / "skills", dest / "skills", ignore=shutil.ignore_patterns("openclave.json"))
    try:
        from config import CONFIG
        owner = CONFIG.owner_name
    except Exception:
        owner = ""
    lines = personal_lines(dest, owner)
    print(f"Draft written to {dest}. Fill in the TODOs in agent.json and onboarding.md.")
    if lines:
        print(f"{len(lines)} line(s) to check before committing:")
        print("\n".join("  " + l for l in lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
