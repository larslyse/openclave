"""Throwaway OpenClave for the UI smoke test: run with HOME pointing at a temp
copy of the agent folders (no conversations), fake model and voice replies,
on the port given as argv[1]. Never touches the real vault or ops state."""
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

SERVICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE))
os.chdir(SERVICE)

import main  # noqa: E402
import cleanup, llm_client, chat_service, retrieval, voice_engine, fish_audio_tts, attention, reminders, llm_models, agent_order, web_push, agent_tools, ha_guard, model_updater  # noqa: E402

PORT = int(sys.argv[1])
STATE = Path(os.environ["OPENCLAVE_HOME"]) / "state"


def fake_chat(agent_id, system_prompt, messages, **kw):
    last = messages[-1]["content"] if messages else ""
    if isinstance(last, str) and "stream please" in last:
        # Like an API route: the reply arrives in pieces (request_phase text).
        import request_phase
        for piece in ("Streaming **part one**", ", part two", ", part three."):
            request_phase.add_text(agent_id, piece)
            time.sleep(1.5)
        return "Streaming **part one**, part two, part three."
    time.sleep(1)
    return f"Fake reply from {agent_id}."


for p in (
    patch.object(llm_client, "chat", fake_chat),
    patch.object(llm_client, "maybe_compact_conversation", lambda *a, **k: None),
    patch.object(llm_client, "log_usage", lambda *a, **k: None),
    patch.object(retrieval, "build_context", lambda *a, **k: ""),
    patch.object(voice_engine, "start", lambda: None),
    patch.object(voice_engine, "transcribe", lambda path, language="en": "hello"),
    patch.object(voice_engine, "synthesize", lambda *a, **k: 1.0),
    patch.object(fish_audio_tts, "synthesize", lambda *a, **k: None),
    patch.object(attention, "start", lambda *a, **k: None),
    patch.object(reminders, "start", lambda *a, **k: None),
    # Reply pushes would reach the owner's real phone (subscriptions live in the service dir).
    patch.object(web_push, "send_all", lambda *a, **k: 0),
    # Ops state is read-only here: never write the real selection/settings files.
    patch.object(llm_models, "set_selected_model", lambda *a, **k: None),
    # Ops state stays in this test instance's OPENCLAVE_HOME/state.
    patch.object(agent_order, "ORDER_FILE", STATE / "agent_order.json"),
    patch.object(agent_tools, "SELECTION_FILE", STATE / "agent_tools.json"),
    patch.object(ha_guard, "STATE_FILE", STATE / "ha_approvals.json"),
    patch.object(cleanup, "start", lambda *a, **k: None),
    patch.object(cleanup, "STATE_FILE", STATE / "cleanup_state.json"),
    patch.object(cleanup, "_model_name", lambda u, r: "Suggested name"),
    patch.object(model_updater, "start", lambda: None),  # no real provider probes from a test server
    patch.object(model_updater, "STATE_FILE", STATE / "auto_models.json"),
):
    p.start()
main.TRUSTED_ORIGINS.add(f"http://127.0.0.1:{PORT}")

import uvicorn  # noqa: E402
uvicorn.run(main.app, host="127.0.0.1", port=PORT, log_level="warning")
