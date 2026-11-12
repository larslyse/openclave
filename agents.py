"""Agent registry loaded from each agent's own ``agent.json`` file."""
import json
from dataclasses import dataclass, field
from typing import Any

from config import CONFIG
from state_io import save_json

AGENTS_ROOT = CONFIG.agents_root
AGENT_FILE = "agent.json"


@dataclass(frozen=True)
class AgentConfig:
    id: str
    name: str
    folder: str
    color: str
    tagline: str
    former_names: tuple[str, ...] = ()
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    cli_tools: tuple[str, ...] = ()
    voice: dict[str, Any] | None = None
    roles: tuple[str, ...] = ()
    connections: dict[str, dict[str, Any]] = field(default_factory=dict)


AGENTS: dict[str, AgentConfig] = {}
MANAGER = "manager"
LEGACY_MANAGER = "maintainer"


def _agent_config(data: dict, folder: str) -> AgentConfig:
    agent_id = data["id"]
    if not isinstance(agent_id, str) or not agent_id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in agent_id):
        raise ValueError("invalid agent id")
    servers = data.get("mcp_servers", {})
    voice = data.get("voice")
    cli_tools = data.get("cli_tools", [])
    roles = data.get("roles", [])
    connections = data.get("connections", {})
    former_names = data.get("former_names", [])
    if not isinstance(servers, dict) or not all(isinstance(v, dict) for v in servers.values()):
        raise ValueError("mcp_servers must be a table of server tables")
    if not isinstance(connections, dict) or not all(isinstance(v, dict) for v in connections.values()):
        raise ValueError("connections must be a table of connection tables")
    if voice is not None and not isinstance(voice, dict):
        raise ValueError("voice must be a table")
    for label, value in (("cli_tools", cli_tools), ("roles", roles), ("former_names", former_names)):
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ValueError(f"{label} must be a list of non-empty strings")
    # Every install has exactly one manager: the agent
    # that keeps OpenClave tidy, runs the weekly audit and is the one way back
    # from agent workspaces into the main vault. "maintainer" is its older name
    # and is still read; both names are set, so either check works.
    if MANAGER in roles or LEGACY_MANAGER in roles:
        roles = list(dict.fromkeys([*roles, MANAGER, LEGACY_MANAGER]))
    return AgentConfig(
        id=agent_id,
        name=str(data["name"]),
        folder=folder,
        color=str(data["color"]),
        tagline=str(data["tagline"]),
        former_names=tuple(former_names),
        mcp_servers=servers,
        cli_tools=tuple(cli_tools),
        voice=voice,
        roles=tuple(roles),
        connections=connections,
    )


def _load_created_agents() -> None:
    """Load every agent definition from the configured agents directory."""
    loaded = {}
    for f in sorted(AGENTS_ROOT.glob(f"*/{AGENT_FILE}")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            cfg = _agent_config(data, f.parent.name)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if cfg.id not in loaded:
            loaded[cfg.id] = cfg
    AGENTS.clear()
    AGENTS.update(loaded)


def register(cfg: AgentConfig) -> None:
    """Mutate the shared registry so newly created agents are available now."""
    AGENTS[cfg.id] = cfg


def with_role(role: str) -> tuple[str, ...]:
    """Return configured agent ids that have a role."""
    return tuple(agent_id for agent_id, cfg in AGENTS.items() if role in cfg.roles)


def has_role(agent_id: str, role: str) -> bool:
    cfg = AGENTS.get(agent_id)
    return cfg is not None and role in cfg.roles


def manager_id() -> str | None:
    """The manager (role "manager", formerly "maintainer"), if there is one."""
    return next(iter(with_role(MANAGER) or with_role(LEGACY_MANAGER)), None)


maintainer_id = manager_id  # older name


def manager_problem() -> str | None:
    """None when there is exactly one manager; else what's wrong, for the
    dashboard banner. OpenClave always needs one."""
    found = tuple(dict.fromkeys(with_role(MANAGER) + with_role(LEGACY_MANAGER)))
    if not AGENTS:
        return None  # a fresh install: nothing to manage yet
    if not found:
        return ("No manager: OpenClave needs one agent with the manager role (cleanup, weekly audit, "
                "the way back into your vault). Add the Manager from Ready-made agents, or add \"manager\" "
                "to one agent's roles in its agent.json.")
    if len(found) > 1:
        names = ", ".join(AGENTS[a].name for a in found)
        return f"More than one manager ({names}): keep the manager role on one agent only."
    return None


def is_home_assistant_agent(agent_id: str) -> bool:
    cfg = AGENTS.get(agent_id)
    return bool(cfg and "home_assistant" in cfg.connections)


def is_onboarding(agent_id: str) -> bool:
    cfg = AGENTS.get(agent_id)
    if cfg is None:
        return False
    try:
        return bool(json.loads((AGENTS_ROOT / cfg.folder / AGENT_FILE).read_text(encoding="utf-8")).get("onboarding"))
    except (OSError, ValueError):
        return False


def set_onboarding(agent_id: str, onboarding: bool) -> None:
    f = AGENTS_ROOT / AGENTS[agent_id].folder / AGENT_FILE
    data = json.loads(f.read_text(encoding="utf-8"))
    data["onboarding"] = onboarding
    save_json(f, data)


_load_created_agents()
