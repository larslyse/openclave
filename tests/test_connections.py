"""connections.py: what an agent loses on a model, and every place that must
say so, so nobody has to remember it."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import attention
import chat_service
import connections
import llm_models
import main
from routes import models as model_routes
from schemas import SetModelRequest

CLI = next(m.id for m in llm_models.CATALOG if m.provider == "claude-cli")
CODEX = next(m.id for m in llm_models.CATALOG if m.provider == "codex-cli")


class ConnectionsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.selection = Path(tmp.name) / "agent_models.json"
        self.selection.write_text(json.dumps({"r2": CODEX, "spice": CODEX}))
        patcher = patch.object(llm_models, "SELECTION_FILE", self.selection)  # never the real file
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_r2_has_home_assistant_on_claude_and_codex(self):
        self.assertEqual(connections.needed("r2"), ["Home Assistant"])
        self.assertEqual(connections.lost("r2", CODEX), [])
        self.assertEqual(connections.lost("r2", CLI), [])
        self.assertEqual(connections.lost("spice", CODEX), [], "agents without connections lose nothing")
        self.assertEqual(connections.supported_by_model("r2").get(CLI), ["HA"], "tag reads 'HA supported'")
        self.assertEqual(connections.supported_by_model("r2").get(CODEX), ["HA"])
        self.assertEqual(connections.supported_by_model("spice"), {})

    def test_connection_not_set_up_is_not_a_warning(self):
        with patch.object(connections, "ready", return_value=False):
            self.assertEqual(connections.lost("reba", CODEX), [], "switching model wouldn't help")
            self.assertEqual(connections.status("reba")[0]["ready"], False)
        with patch.object(connections, "ready", return_value=True):
            self.assertEqual(connections.lost("reba", CODEX), ["Garmin"])

    def test_switching_never_blocks(self):
        self.selection.write_text(json.dumps({"r2": CLI}))
        result = model_routes._set_model("r2", SetModelRequest(model_id=CODEX))
        self.assertEqual(result["loses"], [])
        self.assertEqual(llm_models.selected_model_id("r2"), CODEX)
        with self.assertRaises(HTTPException) as bad:
            model_routes._set_model("r2", SetModelRequest(model_id="no-such-model"))
        self.assertEqual(bad.exception.status_code, 400)

    def test_pickers_attention_and_prompt_all_say_it(self):
        self.assertEqual(model_routes._model_option(llm_models.get(CLI), "r2").supports, ["HA"])
        self.assertEqual(model_routes._model_option(llm_models.get(CODEX), "r2").supports, ["HA"])
        findings = attention.check("r2")
        self.assertNotIn("connection", {f["kind"] for f in findings})
        line = chat_service._connections_line("r2")
        self.assertEqual(line, "")
        self.selection.write_text(json.dumps({"r2": CLI}))
        self.assertNotIn("connection", {f["kind"] for f in attention.check("r2")})
        self.assertEqual(chat_service._connections_line("r2"), "")


if __name__ == "__main__":
    unittest.main()
