"""Skills: reusable know-how an agent loads when a request needs it (T2).

A skill is a folder with a ``SKILL.md``: YAML front matter with ``name`` and
``description``, then the instructions. Same layout as OpenClaw and Claude
skills, so they move between them. Two scopes:

  - shared: ``<skills_root>/<name>/`` (config ``paths.skills_root``), for every agent
  - own:    ``<agent folder>/skills/<name>/``, for that agent only; agents
            write these themselves (save_skill) after a job goes well

The agent's prompt lists only names and descriptions (prompt_line); the
read_skill tool loads the full text when a request matches. That keeps the
prompt small and works the same on every model route.

Imported skills (import_skill) were written for another harness, often one
with a shell, scripts and its own tools. Each import is checked for what it
expects (analyze) and arrives switched off until the owner enables it:
their text becomes instructions an agent follows, so nothing from outside
runs without a look first. The record of an import, its warnings and its
approval, sits next to it in ``openclave.json``.
"""
import json
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import yaml

import vault
from agents import AGENTS
from config import CONFIG
from state_io import load_json, save_json

SKILL_FILE = "SKILL.md"
META_FILE = "openclave.json"
SELECTION_FILE = CONFIG.state_path("agent_skills.json")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_SKILL_CHARS = 40_000
MAX_DESCRIPTION_CHARS = 300
MAX_LISTED = 40
MAX_IMPORT_FILES = 60
MAX_IMPORT_BYTES = 2_000_000
MAX_FILE_READ = 100_000
SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".js", ".mjs", ".ts", ".rb", ".pl", ".ps1", ".bat", ".exe"}
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".csv"} | SCRIPT_SUFFIXES
_SHELL_FENCE = re.compile(r"```(?:bash|sh|shell|zsh|console|terminal|powershell|cmd)\b", re.IGNORECASE)


class SkillError(ValueError):
    pass


def shared_root() -> Path:
    return CONFIG.skills_root


def own_root(agent_id: str) -> Path:
    return vault.agent_root(agent_id) / "skills"


def slug(name: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]+", "-", str(name).strip().lower())).strip("-")[:63]


def parse(text: str) -> tuple[dict, str]:
    """(front matter, body). Unreadable front matter counts as none."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            try:
                meta = yaml.safe_load(text[3:end]) or {}
            except yaml.YAMLError:
                meta = {}
            return (meta if isinstance(meta, dict) else {}), text[end + 4:].lstrip("\n")
    return {}, text


def _meta(folder: Path) -> dict:
    try:
        data = json.loads((folder / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _describe(folder: Path, scope: str) -> dict | None:
    f = folder / SKILL_FILE
    if not f.is_file():
        return None
    front, _ = parse(f.read_text(encoding="utf-8", errors="replace"))
    meta = _meta(folder)
    name = folder.name
    return {
        "key": f"{scope}/{name}", "name": name, "scope": scope,
        "title": str(front.get("name") or name),
        "description": str(front.get("description") or "").strip()[:MAX_DESCRIPTION_CHARS],
        "source": meta.get("source"), "imported": meta.get("imported"),
        "warnings": list(meta.get("warnings") or []),
        # Written here (by the owner or an agent): approved. Imported: only once enabled.
        "approved": bool(meta.get("approved", not meta.get("source"))),
        "files": sorted(str(p.relative_to(folder)) for p in folder.rglob("*")
                        if p.is_file() and p.name not in (SKILL_FILE, META_FILE)),
    }


def _scan(root: Path, scope: str) -> list[dict]:
    if not root.is_dir():
        return []
    return [s for s in (_describe(d, scope) for d in sorted(root.iterdir()) if d.is_dir()) if s]


def _selection() -> dict:
    data = load_json(SELECTION_FILE, {})
    return data if isinstance(data, dict) else {}


def list_for(agent_id: str) -> list[dict]:
    """Every skill this agent can see: its own first, then shared ones.
    ``enabled`` = approved and not switched off for this agent."""
    off = set(_selection().get(agent_id, {}).get("off", []))
    out = []
    for s in _scan(own_root(agent_id), "own") + _scan(shared_root(), "shared"):
        s["enabled"] = s["approved"] and s["key"] not in off
        out.append(s)
    return out


def enabled_for(agent_id: str) -> list[dict]:
    return [s for s in list_for(agent_id) if s["enabled"]]


def set_enabled(agent_id: str, key: str, on: bool) -> dict:
    skill = _find(agent_id, key)
    if on and not skill["approved"]:
        raise SkillError("this imported skill hasn't been enabled yet: review it first")
    data = _selection()
    off = set(data.get(agent_id, {}).get("off", []))
    (off.discard if on else off.add)(key)
    data[agent_id] = {"off": sorted(off)}
    save_json(SELECTION_FILE, data)
    return {**skill, "enabled": on}


def _folder(agent_id: str, key: str) -> Path:
    scope, _, name = str(key).partition("/")
    if scope not in ("own", "shared") or not NAME_RE.match(name):
        raise SkillError(f"unknown skill {key!r}")
    return (own_root(agent_id) if scope == "own" else shared_root()) / name


def _find(agent_id: str, key: str) -> dict:
    skill = next((s for s in list_for(agent_id) if s["key"] == key), None)
    if skill is None:
        raise SkillError(f"no skill {key!r}")
    return skill


def resolve(agent_id: str, name: str) -> dict:
    """An enabled skill by its plain name (own wins over shared) or key."""
    name = str(name or "").strip()
    for s in enabled_for(agent_id):
        if name in (s["key"], s["name"], s["title"]):
            return s
    raise SkillError(f"no enabled skill called {name!r}; the list in your instructions has the names")


def text(agent_id: str, key: str) -> str:
    return (_folder(agent_id, key) / SKILL_FILE).read_text(encoding="utf-8", errors="replace")


def read(agent_id: str, name: str, file: str | None = None) -> str:
    """read_skill: the skill's instructions (or one of its files) for the agent."""
    skill = resolve(agent_id, name)
    folder = _folder(agent_id, skill["key"])
    if file:
        target = (folder / file).resolve()
        if folder.resolve() not in target.parents or not target.is_file():
            raise SkillError(f"{file!r} is not a file in this skill (files: {', '.join(skill['files']) or 'none'})")
        return target.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_READ]
    _, body = parse(text(agent_id, skill["key"]))
    head = f"# Skill: {skill['title']}\n"
    if skill["warnings"]:
        head += ("Imported from another harness. It expects things you may not have: "
                 + "; ".join(skill["warnings"]) + ". Use only the parts you can do with your own tools, "
                 "and tell the owner plainly what you couldn't do.\n")
    if skill["files"]:
        head += f"Other files in this skill (read_skill with file=...): {', '.join(skill['files'])}\n"
    return head + "\n" + body


def prompt_line(agent_id: str) -> str:
    """For the agent's prompt: names and descriptions only. Never raises."""
    try:
        listed = enabled_for(agent_id)[:MAX_LISTED]
    except Exception:
        return ""
    if not listed:
        return ""
    lines = "\n".join(f"- {s['name']}: {s['description'] or s['title']}" for s in listed)
    return ("\n# Skills\nKnow-how written down for jobs that come up. When a request or task matches one, "
            "load it with read_skill(name) before you start, and follow it.\n" + lines + "\n")


def _skill_markdown(name: str, description: str, body: str) -> str:
    front = yaml.safe_dump({"name": name, "description": description}, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{front}\n---\n\n{body.strip()}\n"


def save_own(agent_id: str, name: str, description: str, body: str) -> dict:
    """save_skill: the agent writes or rewrites one of its own skills."""
    name = slug(name)
    description = " ".join(str(description or "").split())
    body = str(body or "").strip()
    if not NAME_RE.match(name):
        raise SkillError("give the skill a short name, like 'ha-update'")
    if not description:
        raise SkillError("the description says when to use the skill; it can't be empty")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillError(f"keep the description under {MAX_DESCRIPTION_CHARS} characters")
    if not body:
        raise SkillError("the skill needs instructions")
    if len(body) > MAX_SKILL_CHARS:
        raise SkillError(f"keep the skill under {MAX_SKILL_CHARS} characters")
    folder = own_root(agent_id) / name
    if _meta(folder).get("source"):
        raise SkillError(f"'{name}' is an imported skill; save yours under another name")
    path = folder / SKILL_FILE
    with vault.file_lock(agent_id, path) as scoped:
        vault._write_text_unlocked(scoped, _skill_markdown(name, description, body))
    return _describe(folder, "own")


# ── Import ──────────────────────────────────────────────────────────────

def analyze(skill_md: str, files: list[str]) -> list[str]:
    """What a skill expects that OpenClave agents don't have: no shell, no
    scripts, only their own tools. Empty = plain know-how, safe to use."""
    front, body = parse(skill_md)
    warnings = []
    scripts = [f for f in files if Path(f).suffix.lower() in SCRIPT_SUFFIXES]
    if scripts:
        warnings.append(f"comes with scripts agents can't run ({', '.join(scripts[:5])}"
                        f"{', …' if len(scripts) > 5 else ''})")
    if _SHELL_FENCE.search(body) or re.search(r"(?m)^\s*\$ \S", body):
        warnings.append("tells the agent to run shell commands")
    meta = front.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    requires = {}
    for value in (meta or {}).values() if isinstance(meta, dict) else ():
        if isinstance(value, dict) and isinstance(value.get("requires"), dict):
            requires = value["requires"]
    bins = [str(b) for key in ("bins", "anyBins") for b in (requires.get(key) or [])]
    if bins:
        warnings.append(f"needs programs installed ({', '.join(bins[:6])})")
    envs = [str(e) for e in (requires.get("env") or [])]
    if envs:
        warnings.append(f"needs settings or keys ({', '.join(envs[:6])})")
    tools = front.get("allowed-tools") or front.get("allowed_tools")
    if tools:
        listed = tools if isinstance(tools, list) else re.split(r"[,\s]+", str(tools))
        warnings.append(f"expects its harness's tools ({', '.join(t for t in listed if t)[:120]})")
    if re.search(r"\bopenclaw\b", skill_md, re.IGNORECASE) and not any("harness" in w for w in warnings):
        warnings.append("written for OpenClaw; tool names and paths may not apply")
    return warnings


def _github_parts(url: str):
    """(owner, repo, ref, path, kind) for a github.com blob/tree URL."""
    p = urllib.parse.urlsplit(url)
    parts = [x for x in p.path.split("/") if x]
    if p.netloc.lower() not in ("github.com", "www.github.com") or len(parts) < 4 or parts[2] not in ("blob", "tree"):
        return None
    return parts[0], parts[1], parts[3], "/".join(parts[4:]), parts[2]


def _fetch(url: str, limit: int = MAX_IMPORT_BYTES) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "OpenClave-skill-import", "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 -- https/http only, checked by caller
        data = r.read(limit + 1)
    if len(data) > limit:
        raise SkillError("that download is too big for a skill")
    return data


def _collect_github(url: str, dest: Path) -> None:
    owner, repo, ref, path, kind = _github_parts(url)
    if kind == "blob":
        raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
        if path.endswith(SKILL_FILE):
            (dest / SKILL_FILE).write_bytes(_fetch(raw))
            return
        raise SkillError("link to a SKILL.md file or to the skill's folder")
    budget = {"files": 0, "bytes": 0}

    def walk(sub: str, into: Path):
        api = f"https://api.github.com/repos/{owner}/{repo}/contents/{urllib.parse.quote(sub)}?ref={urllib.parse.quote(ref)}"
        for item in json.loads(_fetch(api)):
            if item.get("type") == "dir":
                walk(item["path"], into / item["name"])
            elif item.get("type") == "file" and Path(item["name"]).suffix.lower() in TEXT_SUFFIXES:
                budget["files"] += 1
                budget["bytes"] += int(item.get("size") or 0)
                if budget["files"] > MAX_IMPORT_FILES or budget["bytes"] > MAX_IMPORT_BYTES:
                    raise SkillError("that folder is too big for a skill")
                into.mkdir(parents=True, exist_ok=True)
                (into / item["name"]).write_bytes(_fetch(item["download_url"]))
    walk(path, dest)


def _collect_local(source: Path, dest: Path) -> None:
    count = size = 0
    for f in sorted(source.rglob("*")):
        rel = f.relative_to(source)
        if not f.is_file() or any(part.startswith(".") for part in rel.parts):
            continue
        if f.suffix.lower() not in TEXT_SUFFIXES and f.name != SKILL_FILE:
            continue
        count += 1
        size += f.stat().st_size
        if count > MAX_IMPORT_FILES or size > MAX_IMPORT_BYTES:
            raise SkillError("that folder is too big for a skill")
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, dest / rel)


def _stage(source: str, pasted: str | None) -> tuple[Path, str]:
    """Download or copy the skill into a temporary folder. Returns (folder, source label)."""
    tmp = Path(tempfile.mkdtemp(prefix="openclave-skill-"))
    if pasted and pasted.strip():
        (tmp / SKILL_FILE).write_text(pasted, encoding="utf-8")
        return tmp, "pasted text"
    source = str(source or "").strip()
    if not source:
        raise SkillError("give a link, a folder on this server, or paste the SKILL.md text")
    if source.startswith(("http://", "https://")):
        if _github_parts(source):
            _collect_github(source, tmp)
        elif source.split("?")[0].endswith(".md"):
            (tmp / SKILL_FILE).write_bytes(_fetch(source))
        else:
            raise SkillError("links work to a SKILL.md file, or to a GitHub folder or file")
        return tmp, source
    path = Path(source).expanduser()
    if path.is_file() and path.name == SKILL_FILE:
        path = path.parent
    if not (path / SKILL_FILE).is_file():
        raise SkillError(f"no {SKILL_FILE} in {path}")
    _collect_local(path, tmp)
    return tmp, str(path)


def import_skill(source: str = "", *, pasted: str | None = None, target: str = "shared",
                 name: str | None = None, preview: bool = False, replace: bool = False) -> dict:
    """Import one skill into the shared library or one agent's folder.
    preview=True only reports what it found. Imports start switched off."""
    staged, label = _stage(source, pasted)
    try:
        if not (staged / SKILL_FILE).is_file():
            raise SkillError(f"no {SKILL_FILE} found there")
        skill_md = (staged / SKILL_FILE).read_text(encoding="utf-8", errors="replace")
        if len(skill_md) > MAX_SKILL_CHARS:
            raise SkillError(f"{SKILL_FILE} is over {MAX_SKILL_CHARS} characters")
        front, body = parse(skill_md)
        if not body.strip():
            raise SkillError(f"{SKILL_FILE} has no instructions")
        files = sorted(str(p.relative_to(staged)) for p in staged.rglob("*") if p.is_file() and p.name != SKILL_FILE)
        final = slug(name or front.get("name") or (Path(label).name if label != "pasted text" else ""))
        if not NAME_RE.match(final):
            raise SkillError("the skill has no usable name; give one")
        if target != "shared" and target not in AGENTS:
            raise SkillError(f"unknown agent {target!r}")
        dest = (shared_root() if target == "shared" else own_root(target)) / final
        report = {"name": final, "title": str(front.get("name") or final),
                  "description": str(front.get("description") or "").strip()[:MAX_DESCRIPTION_CHARS],
                  "warnings": analyze(skill_md, files), "files": files, "source": label,
                  "target": target, "exists": dest.exists()}
        if not report["description"]:
            report["warnings"].append("has no description, so agents won't know when to use it")
        if preview:
            return report
        if dest.exists() and not replace:
            raise SkillError(f"a skill called '{final}' is already there")
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staged, dest)
        (dest / META_FILE).write_text(json.dumps({
            "source": label, "imported": date.today().isoformat(),
            "warnings": report["warnings"], "approved": False}, indent=2) + "\n", encoding="utf-8")
        return {**report, "key": f"{'shared' if target == 'shared' else 'own'}/{final}", "approved": False}
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def approve(agent_id: str, key: str) -> dict:
    """The owner read an imported skill and enables it."""
    folder = _folder(agent_id, key)
    meta = _meta(folder)
    if not meta:
        raise SkillError("only imported skills need enabling")
    meta["approved"] = True
    save_json(folder / META_FILE, meta)
    return set_enabled(agent_id, key, True)


def delete(agent_id: str, key: str) -> None:
    folder = _folder(agent_id, key)
    if not (folder / SKILL_FILE).is_file():
        raise SkillError(f"no skill {key!r}")
    shutil.rmtree(folder)
