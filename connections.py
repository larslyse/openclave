"""Which agent needs which outside connection, and which models can provide it.

The one place that answers "which models can this agent use its connections
on?", so nobody has to remember it. Everything else reads from here:
  - the model pickers tag the models that support the agent's connections,
    e.g. "HA supported" (positive only: no "no ..." noise)
  - the agent's own prompt says when a connection is unavailable, so it
    tells the owner they forgot and which models to use
  - attention.py flags an agent already on a model without it
  - Control Panel → Tools lists the connections and their state

Agents describe each outside connection in their ``agent.json``: display
names, model routes, and any readiness requirement stay with that agent.
The API routes only have OpenClave's own tools.
"""
import agents
import llm_models


def route_hint(agent_id: str) -> str:
    hints = []
    for connection in _definitions(agent_id):
        label = str(connection.get("label", "Connection"))
        routes = connection.get("routes", [])
        if routes:
            hints.append(f"{label} works with {', '.join(routes)}")
    return "; ".join(hints) or "No outside connections are configured"


def short_label(agent_id: str, name: str) -> str:
    return next((str(c.get("short", name)) for c in _definitions(agent_id) if c.get("label") == name), name)


def _definitions(agent_id: str) -> list[dict]:
    cfg = agents.AGENTS.get(agent_id)
    return list(cfg.connections.values()) if cfg else []


def needed(agent_id: str) -> list[str]:
    out = []
    for connection in _definitions(agent_id):
        label = str(connection.get("label", "Connection"))
        if label not in out:
            out.append(label)
    return out


def model_supports(model_id: str, name: str, agent_id: str | None = None) -> bool:
    provider = llm_models.get(model_id).provider
    return any(c.get("label") == name and provider in c.get("routes", [])
               for c in _definitions(agent_id) if agent_id is not None)


def ready(name: str, agent_id: str | None = None) -> bool:
    config = next((c for c in _definitions(agent_id) if c.get("label") == name), {}) if agent_id else {}
    requirement = config.get("ready")
    if requirement == "garmin_token_dir":
        from config import CONFIG
        return CONFIG.garmin_token_dir.exists()
    return True


def lost(agent_id: str, model_id: str) -> list[str]:
    """Set-up connections this agent can't use on this model. One that isn't
    set up yet is lost on every model, so switching wouldn't help."""
    return [n for n in needed(agent_id) if ready(n, agent_id) and not model_supports(model_id, n, agent_id)]


def lost_now(agent_id: str) -> list[str]:
    return lost(agent_id, llm_models.selected_model_id(agent_id))


def supported(agent_id: str, model_id: str) -> list[str]:
    """Short names of the agent's (set-up) connections this model supports,
    for the picker tag: ["HA"] → "HA supported"."""
    short = {str(c.get("label")): str(c.get("short", c.get("label", ""))) for c in _definitions(agent_id)}
    return [short.get(n, n) for n in needed(agent_id) if ready(n, agent_id) and model_supports(model_id, n, agent_id)]


def status(agent_id: str) -> list[dict]:
    """For Control Panel → Tools: each connection, whether the current model
    can use it, and whether it's set up."""
    model_id = llm_models.selected_model_id(agent_id)
    return [{"name": name, "available": model_supports(model_id, name, agent_id), "ready": ready(name, agent_id)}
            for name in needed(agent_id)]


def supported_by_model(agent_id: str) -> dict[str, list[str]]:
    """model_id → supported connection tags, only for models that have some."""
    if not needed(agent_id):
        return {}
    return {m.id: supported(agent_id, m.id) for m in llm_models.CATALOG if supported(agent_id, m.id)}
