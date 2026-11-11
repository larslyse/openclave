"""Which of llm_client.TOOLS each agent gets.

Every agent always has CORE_TOOLS (reading the vault, keeping its own
mission/persona/plan/backlog, reaching the maintainer). The rest are optional and chosen
per agent: by the agent itself at the end of onboarding, or by the owner in the
Control Panel. An agent with no saved selection gets every tool — that's how
the pre-existing agents keep working unchanged until someone trims them.

Ops state, not agent knowledge: stored under OPENCLAVE_HOME/state.
Read fresh per call, so a Control Panel change applies to the next message.
"""

import agents
from config import CONFIG
from state_io import load_json, save_json

SELECTION_FILE = CONFIG.state_path("agent_tools.json")

CORE_TOOLS = (
    "search_vault", "read_vault_file", "read_note", "list_notes", "read_memory", "list_memories",
    "get_foundations", "read_own_dashboard", "update_backlog", "update_plan_phase",
    "update_mission", "update_persona", "add_feedback", "flag_to_maintainer", "check_maintainer_request", "message_agent",
    "check_page", "write_page", "restore_page", "find_voice", "set_voice", "read_skill", "save_skill",
)
OPTIONAL_TOOLS = (
    "write_note", "write_memory", "add_foundation", "log_measurement", "get_measurements",
    "write_own_dashboard", "update_maintainer_inbox_item", "schedule_reminder", "make_pdf", "write_vault_note", "web_search",
)
# Optional capabilities that aren't function tools in llm_client.TOOLS: the
# route provides them (Codex live search, claude-cli WebSearch/WebFetch).
# Listed here so the Control Panel and onboarding can offer them like tools.
CAPABILITIES = {
    "web_search": "Search the live web for current facts, specs, prices and events. "
                  "Off for agents configured to act on the real world, since a web page could steer them.",
}
ONBOARDING_TOOL = "finish_onboarding"
MAINTAINER_ONLY = {"update_maintainer_inbox_item"}
# Always on for the manager, never for anyone else: how it asks before acting.
MANAGER_TOOLS = ("propose_to_owner",)
# On for agents in a clave while claves are on (claves.backlog_tool).
CLAVE_TOOL = "clave_backlog"


def _load() -> dict[str, list[str]]:
    data = load_json(SELECTION_FILE, {})
    return data if isinstance(data, dict) else {}


def optional_for(agent_id: str) -> tuple[str, ...]:
    return tuple(t for t in OPTIONAL_TOOLS if t not in MAINTAINER_ONLY or agents.has_role(agent_id, "maintainer"))


def selected_optional(agent_id: str) -> list[str] | None:
    """None = no selection saved (the agent gets everything)."""
    chosen = _load().get(agent_id)
    return None if chosen is None else [t for t in chosen if t in optional_for(agent_id)]


def allowed(agent_id: str) -> set[str]:
    chosen = selected_optional(agent_id)
    tools = set(CORE_TOOLS) | set(optional_for(agent_id) if chosen is None else chosen)
    if agents.is_onboarding(agent_id):
        tools.add(ONBOARDING_TOOL)
    if agents.has_role(agent_id, agents.MANAGER):
        tools.update(MANAGER_TOOLS)
    import claves
    if claves.claves_of(agent_id):
        tools.add(CLAVE_TOOL)  # its clave's shared backlog
    return tools


def has_web_search(agent_id: str) -> bool:
    return "web_search" in allowed(agent_id)


def set_optional(agent_id: str, tools: list[str]) -> list[str]:
    unknown = set(tools) - set(optional_for(agent_id))
    if unknown:
        raise ValueError(f"not optional tools for this agent: {', '.join(sorted(unknown))}")
    data = _load()
    data[agent_id] = [t for t in optional_for(agent_id) if t in tools]
    save_json(SELECTION_FILE, data)
    return data[agent_id]
