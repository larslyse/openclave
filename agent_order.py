"""Dashboard tile order in OPENCLAVE_HOME/state (host UI state, not vault data).
"""
import json

from config import CONFIG
from agents import AGENTS

ORDER_FILE = CONFIG.state_path("agent_order.json")


def get_order() -> list[str]:
    """Every current agent id, in the saved order. An agent added to the
    registry after the file was last saved is appended at the end rather than
    dropped; an id no longer in AGENTS (an agent removed) is dropped."""
    saved = []
    if ORDER_FILE.exists():
        try:
            saved = json.loads(ORDER_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            saved = []
    known = [a for a in saved if a in AGENTS]
    return known + [a for a in AGENTS if a not in known]


def set_order(order: list[str]) -> None:
    """The full order, or the order of some agents (Boxes or tabs showing one
    clave): those take the places they had, in their new order, and the
    others stay where they were."""
    if len(set(order)) != len(order) or not set(order) <= set(AGENTS) or not order:
        raise ValueError("order must list current agent ids, each once")
    full = get_order()
    wanted = set(order)
    slots = [i for i, a in enumerate(full) if a in wanted]
    for i, agent_id in zip(slots, order):
        full[i] = agent_id
    ORDER_FILE.write_text(json.dumps(full, indent=2))
