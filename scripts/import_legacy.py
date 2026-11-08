"""Import an install of the old single-user OpenClave service (I5.1).

The old service hardcoded its built-in agents, their MCP servers, CLI tools
and voices in code, kept runtime state in its code directory, and shipped
built-in avatars and phone pages. This writes all of that as data:

  - each agent's ``agent.json`` gets the fields the old code supplied
    (colour, tagline, roles, connections, MCP servers, CLI tools, voice).
    Fields already in the file win; nothing is removed, so the old service
    still reads the same files if you roll back;
  - built-in avatars are copied into the agent folders, uploaded ones into
    ``$OPENCLAVE_HOME/state/custom_avatars``;
  - an agent that relied on a built-in phone page and has no ``page.json``
    gets one;
  - runtime state files are copied into ``$OPENCLAVE_HOME/state``;
  - ``openclave.toml`` is written only if missing, pointing at the agents
    folder in place (it is not copied).

Reads the old code with ``ast`` only; nothing from it is executed. Dry run
by default: pass ``--apply`` to write. Running it again changes nothing.

    python3 scripts/import_legacy.py --old-service ~/old-service \\
        --agents-root ~/notes/OpenClave/Agents [--home ~/.openclave] [--apply]
"""
import argparse
import ast
import json
import shutil
import sys
from pathlib import Path

STATE_FILES = (
    "agent_models.json", "agent_order.json", "agent_tools.json", "auto_models.json", "claude_auth.json",
    "cleanup_state.json", "cli_quota.json", "digest_state.json", "ha_approvals.json", "model_check.json",
    "push_keys.json", "push_subscriptions.json", "service_settings.json", "usage_log.jsonl",
)
# The old maintainer was hardcoded by id.
LEGACY_ROLES = {"rick": ["maintainer"]}
# Old built-in phone pages, as page.json (custom_pages.py block types).
LEGACY_PAGES = {
    "rocky": {"title": "Plan", "blocks": [{"type": "markdown", "title": "Current plan", "file": "plan.md"},
                                          {"type": "markdown", "title": "Backlog", "file": "backlog.md"}]},
    "r2": {"title": "Plan", "blocks": [{"type": "markdown", "title": "Current plan", "file": "plan.md"},
                                       {"type": "markdown", "title": "Backlog", "file": "backlog.md"}]},
    "reba": {"title": "Progress", "blocks": [{"type": "sliders", "title": "Progress and pain", "metrics": [
        {"id": "dorsiflexion", "label": "Dorsiflexion", "min": 6, "max": 15, "step": 0.5, "unit": "cm"},
        {"id": "dorsi_hold", "label": "DF hold", "min": 0, "max": 60, "step": 1, "unit": "s"},
        {"id": "pf", "label": "PF", "min": 0, "max": 10, "step": 1},
        {"id": "hip", "label": "hip", "min": 0, "max": 10, "step": 1},
        {"id": "lower_back", "label": "lower back", "min": 0, "max": 10, "step": 1},
        {"id": "elbow", "label": "elbow", "min": 0, "max": 10, "step": 1}]}]},
}


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _assigned(tree: ast.Module, name: str):
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == name for t in targets) and node.value is not None:
            return node.value
    return None


def _literal(node, default):
    try:
        return ast.literal_eval(node) if node is not None else default
    except ValueError:
        return default


def legacy_agents(old: Path) -> dict[str, dict]:
    """Built-in AgentConfig(...) declarations from the old agents.py."""
    out = {}
    for node in ast.walk(_module(old / "agents.py")):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "AgentConfig":
            fields = {k.arg: _literal(k.value, None) for k in node.keywords if k.arg}
            if all(isinstance(fields.get(k), str) for k in ("id", "name", "folder", "color", "tagline")):
                out[fields["id"]] = fields
    return out


def _string_constants(node) -> list[str]:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def legacy_servers(old: Path) -> dict[str, dict]:
    """Old llm_client.AGENT_EXTRA_MCP_SERVERS, rebuilt as agent-owned server
    data. Its values used f-strings and os.environ, so the known shapes are
    mapped rather than evaluated."""
    tree = _module(old / "llm_client.py")
    table = _assigned(tree, "AGENT_EXTRA_MCP_SERVERS")
    ha_base = "http://homeassistant.local:8123"
    ha_file = old / "mcp_ha_server.py"
    if ha_file.exists():
        base = next((s for s in _string_constants(_assigned(_module(ha_file), "HA_BASE_URL") or ast.Constant(""))
                     if s.startswith("http")), None)
        ha_base = base or ha_base
    out: dict[str, dict] = {}
    if not isinstance(table, ast.Dict):
        return out
    for agent_key, servers in zip(table.keys, table.values):
        agent_id = _literal(agent_key, None)
        if not isinstance(agent_id, str) or not isinstance(servers, ast.Dict):
            continue
        for server_key, server in zip(servers.keys, servers.values):
            name = _literal(server_key, None)
            strings = _string_constants(server)
            data = out.setdefault(agent_id, {"mcp_servers": {}, "connections": {}})
            if name == "ha":
                url = next((s for s in strings if s.startswith("http")), ha_base + "/mcp_server/sse")
                data["mcp_servers"]["ha"] = {"type": "sse", "url": url,
                                             "headers": {"Authorization": "Bearer ${HA_API_TOKEN}"}}
            elif name == "ha-admin":
                data["mcp_servers"]["ha-admin"] = {
                    "command": "${OPENCLAVE_PYTHON}", "args": ["${OPENCLAVE_DIR}/mcp_ha_server.py"],
                    "env": {"HA_API_TOKEN": "${HA_API_TOKEN}", "HA_BASE_URL": ha_base},
                    "codex": {"name": "openclave-ha-admin", "env_vars": ["HA_API_TOKEN"],
                              "default_tools_approval_mode": "auto", "required": True,
                              "read_only_env": "OPENCLAVE_HA_READ_ONLY"}}
            elif name == "garmin":
                data["mcp_servers"]["garmin"] = {"command": "uvx", "args": [s for s in strings if s not in ("command", "args", "uvx")]}
                data["connections"]["garmin"] = {"label": "Garmin", "short": "Garmin", "routes": ["claude-cli"],
                                                 "mcp_servers": ["garmin"], "ready": "garmin_token_dir"}
            else:
                print(f"  ! {agent_id}: unknown legacy MCP server {name!r}, not imported", file=sys.stderr)
                continue
        if agent_id in out and {"ha", "ha-admin"} & set(out[agent_id]["mcp_servers"]):
            out[agent_id]["connections"]["home_assistant"] = {
                "label": "Home Assistant", "short": "HA", "routes": ["claude-cli", "codex-cli"],
                "mcp_servers": [k for k in ("ha", "ha-admin") if k in out[agent_id]["mcp_servers"]]}
    return out


def legacy_cli_tools(old: Path) -> dict[str, list[str]]:
    table = _literal(_assigned(_module(old / "llm_client.py"), "AGENT_TOOLS"), {})
    return {k: [t for t in str(v).split(",") if t] for k, v in table.items()}


def legacy_voices(old: Path) -> dict[str, dict]:
    path = old / "chat_service.py"
    return _literal(_assigned(_module(path), "AGENT_VOICE"), {}) if path.exists() else {}


class Plan:
    def __init__(self, apply: bool):
        self.apply = apply
        self.changes = 0

    def write(self, path: Path, text: str, why: str) -> None:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return
        self.changes += 1
        print(f"  {'wrote' if self.apply else 'would write'} {path}  ({why})")
        if self.apply:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def copy(self, src: Path, dst: Path, why: str) -> None:
        if dst.exists():
            return
        self.changes += 1
        print(f"  {'copied' if self.apply else 'would copy'} {src} -> {dst}  ({why})")
        if self.apply:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def merged_agent(current: dict, builtin: dict | None, extra: dict) -> dict:
    """Fields already in agent.json win; legacy values only fill gaps."""
    out = dict(current)
    for key in ("id", "name", "color", "tagline"):
        if builtin and key not in out:
            out[key] = builtin[key]
    out.setdefault("former_names", list(builtin.get("former_names") or ()) if builtin else [])
    for key, value in extra.items():
        if value and key not in out:
            out[key] = value
    return out


def run(old: Path, agents_root: Path, home: Path, *, apply: bool, vault_root: Path | None = None) -> int:
    plan = Plan(apply)
    builtins = legacy_agents(old)
    servers = legacy_servers(old)
    tools = legacy_cli_tools(old)
    voices = legacy_voices(old)

    print("Agents:")
    folders = {p.parent.name: p for p in agents_root.glob("*/agent.json")}
    for b in builtins.values():
        folders.setdefault(b["folder"], agents_root / b["folder"] / "agent.json")
    for folder, path in sorted(folders.items()):
        if not path.parent.is_dir():
            print(f"  ! {folder}: built-in agent without a folder, skipped")
            continue
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        agent_id = current.get("id") or next((b["id"] for b in builtins.values() if b["folder"] == folder), None)
        if not agent_id:
            print(f"  ! {folder}: agent.json without an id, skipped")
            continue
        builtin = builtins.get(agent_id)
        extra = {"roles": LEGACY_ROLES.get(agent_id), "cli_tools": tools.get(agent_id),
                 "voice": voices.get(agent_id), **servers.get(agent_id, {})}
        merged = merged_agent({**current, "id": agent_id}, builtin, extra)
        missing = [k for k in ("name", "color", "tagline") if k not in merged]
        if missing:
            print(f"  ! {folder}: no {', '.join(missing)} anywhere; add them to agent.json by hand")
            continue
        plan.write(path, json.dumps(merged, indent=2, ensure_ascii=False) + "\n", "agent definition")
        page = path.parent / "page.json"
        if agent_id in LEGACY_PAGES and not page.exists() and not (path.parent / "page.html").exists():
            plan.write(page, json.dumps(LEGACY_PAGES[agent_id], indent=2) + "\n", "replaces the built-in phone page")
        if not any((path.parent / f"avatar{ext}").exists() for ext in (".svg", ".png")):
            for ext in (".svg", ".png"):
                src = old / "static" / "avatars" / f"{agent_id}{ext}"
                if src.exists():
                    plan.copy(src, path.parent / f"avatar{ext}", "built-in avatar")
                    break

    print("Uploaded avatars:")
    for src in sorted((old / "static" / "custom_avatars").glob("*")):
        if src.is_file():
            plan.copy(src, home / "state" / "custom_avatars" / src.name, "uploaded avatar")

    print("Runtime state:")
    for name in STATE_FILES:
        if (old / name).exists():
            plan.copy(old / name, home / "state" / name, "runtime state")

    print("Config:")
    toml = home / "openclave.toml"
    if toml.exists():
        print(f"  {toml} exists; left as is")
    else:
        vault = vault_root or agents_root.parent
        plan.write(toml, (
            "# Written by scripts/import_legacy.py; see openclave.example.toml for every setting.\n"
            "[paths]\n"
            f'vault_root = "{vault}"\n'
            f'agents_root = "{agents_root}"\n'
            f'shared_concept_file = "{agents_root.parent / "openclave.md"}"\n'
        ), "settings")

    print(f"{plan.changes} change{'s' if plan.changes != 1 else ''}" + ("" if apply else " (dry run: pass --apply to write)"))
    return plan.changes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--old-service", type=Path, required=True, help="the old service's code directory")
    parser.add_argument("--agents-root", type=Path, required=True, help="the agents folder (used in place)")
    parser.add_argument("--home", type=Path, default=Path.home() / ".openclave", help="OPENCLAVE_HOME (default ~/.openclave)")
    parser.add_argument("--vault-root", type=Path, help="notes vault root for the new config (default: agents folder's parent)")
    parser.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    args = parser.parse_args(argv)
    old, agents_root = args.old_service.expanduser().resolve(), args.agents_root.expanduser().resolve()
    for path, what in ((old / "agents.py", "old service"), (agents_root, "agents folder")):
        if not path.exists():
            parser.error(f"{what} not found: {path}")
    run(old, agents_root, args.home.expanduser().resolve(), apply=args.apply,
        vault_root=args.vault_root.expanduser().resolve() if args.vault_root else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
