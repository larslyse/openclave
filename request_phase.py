"""Coarse, in-memory, per-agent phase for the "Working for Xs" UI indicator —
something better to show than a static timer during a long reply. Ephemeral
only: not persisted, doesn't survive a restart, and needs no locking since
each agent processes at most one request at a time (chat_service's
agent_execution gate already guarantees that).

Also the reply text so far while an API route streams it (the owner's backlog,
2026-10-13): the same poll shows it growing in the pending bubble. The CLI
routes don't stream, so there it stays empty. Cleared with the phase.
"""
_PHASE: dict[str, str] = {}
_TEXT: dict[str, str] = {}


def set_phase(agent_id: str, phase: str | None) -> None:
    if phase is None:
        _PHASE.pop(agent_id, None)
        _TEXT.pop(agent_id, None)
    else:
        _PHASE[agent_id] = phase


def get_phase(agent_id: str) -> str | None:
    return _PHASE.get(agent_id)


def reset_text(agent_id: str) -> None:
    """A new model call starts: text from a round that ended in tool calls
    isn't part of the reply, so it stops showing."""
    _TEXT.pop(agent_id, None)


def add_text(agent_id: str, delta: str) -> None:
    if delta:
        _TEXT[agent_id] = _TEXT.get(agent_id, "") + delta


def get_text(agent_id: str) -> str:
    return _TEXT.get(agent_id, "")
