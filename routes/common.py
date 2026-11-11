"""Helpers and shared state used by several route modules."""
from pathlib import Path

from fastapi import HTTPException
import claves
from agents import AGENTS

# Every registered agent shares the voice pipeline.
VOICE_ENABLED_AGENTS = set(AGENTS)

STATIC_DIR = Path(__file__).parent.parent / "static"


def _main():
    """The main module, looked up at call time: main imports the route
    modules, and tests patch names on it (TRUSTED_ORIGINS,
    _register_agent_routes) that routes must see."""
    import main
    return main


def _register_agent_routes(agent_id: str) -> None:
    """main._register_agent_routes, looked up when called."""
    _main()._register_agent_routes(agent_id)


def _lan_address() -> str | None:
    """This machine's address on its local network (the interface a packet
    to the internet would leave by; nothing is sent)."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # TEST-NET address: routing lookup only
            address = sock.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") else address


def _skill_agent(agent_id: str) -> None:
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")


def _clave_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Not found: {exc.args[0] if exc.args else ''}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _join_clave(agent_id: str, clave_id) -> None:
    """A new agent made from a clave's "+ New agent" box joins that clave.
    The agent exists by now, so a clave that has gone away is just skipped."""
    if clave_id:
        try:
            claves.place(agent_id, str(clave_id))
        except (KeyError, ValueError):
            pass
