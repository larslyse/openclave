"""The running process sees agents created, renamed and removed without a
restart: agents.AGENTS is changed in place, never rebound, so every module
that did `from agents import AGENTS` holds the same, current dict."""
import importlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import agent_factory
import agent_order
import agent_tools
import agents
import llm_models
import main
import room
import vault

# Every module that imports the registry by name.
HOLDERS = ("agent_messages", "agent_order", "attention", "audit", "channels", "chat_jobs", "chat_service",
           "claves", "cleanup", "handover", "main", "proposals", "reminders", "retrieval", "room",
           "scheduled_tasks", "skills", "vault")


class RegistryReloadTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.root = tmp / "Agents"
        self.root.mkdir()
        for patcher in (
            patch.object(agents, "AGENTS_ROOT", self.root),
            patch.object(vault, "AGENTS_ROOT", self.root),
            patch.object(agent_tools, "SELECTION_FILE", tmp / "agent_tools.json"),
            patch.object(llm_models, "SELECTION_FILE", tmp / "agent_models.json"),
            patch.object(agent_order, "ORDER_FILE", tmp / "agent_order.json"),
            patch.object(agent_factory, "AVATAR_DIR", tmp / "avatars"),
            patch.dict(agents.AGENTS, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)

    def holders(self):
        return {name: importlib.import_module(name).AGENTS for name in HOLDERS}

    def listed(self):
        response = self.client.get("/api/agents")
        self.assertEqual(response.status_code, 200)
        return {a["id"]: a["name"] for a in response.json()}

    def test_every_holder_shares_the_one_registry(self):
        for name, held in self.holders().items():
            self.assertIs(held, agents.AGENTS, f"{name}.AGENTS is a copy")

    def test_create_rename_delete_without_restart(self):
        created = self.client.post("/api/agents", json={
            "name": "Tallyho", "purpose": "Counting things", "model_id": "gpt-5.6-terra"})
        self.assertEqual(created.status_code, 200, created.text)
        agent_id = created.json()["id"]
        for name, held in self.holders().items():
            self.assertIn(agent_id, held, name)
        self.assertEqual(self.listed(), {agent_id: "Tallyho"})
        self.assertEqual(agent_order.get_order(), [agent_id])
        self.assertEqual(room.mentions("hi @tallyho"), [agent_id])
        self.assertEqual(self.client.get(f"/{agent_id}/phase").status_code, 200)  # its routes exist now

        renamed = self.client.post(f"/api/agents/{agent_id}/name", json={"name": "Abacus", "tagline": "Counts"})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        for name, held in self.holders().items():
            self.assertEqual(held[agent_id].name, "Abacus", name)
        self.assertEqual(self.listed(), {agent_id: "Abacus"})
        self.assertEqual(room.mentions("hi @abacus"), [agent_id])
        self.assertEqual(vault.agent_root(agent_id), (self.root / "Tallyho").resolve())  # the folder stays

        # Removing an agent is moving its folder out (scripts/merge_agent.py
        # retires it to _Retired/) and re-reading the agents folder.
        (self.root / "_Retired").mkdir()
        shutil.move(str(self.root / "Tallyho"), str(self.root / "_Retired" / "Tallyho"))
        agents._load_created_agents()
        for name, held in self.holders().items():
            self.assertNotIn(agent_id, held, name)
        self.assertEqual(self.listed(), {})
        self.assertEqual(agent_order.get_order(), [])
        self.assertEqual(room.mentions("hi @abacus"), [])

    def test_clone_is_registered_at_once(self):
        agent_factory.create_agent("Tallyho", "Counting things", "gpt-5.6-terra")
        with patch("claves.get", return_value=type("C", (), {"name": "Work"})()), \
                patch("claves.name_taken_in_clave", return_value=False), \
                patch("claves.place"):
            clone = agent_factory.clone_agent("tallyho", "work")
        self.assertIn(clone["id"], agents.AGENTS)
        self.assertIn(clone["id"], main.AGENTS)
        self.assertEqual(agents.AGENTS[clone["id"]].folder, "Tallyho (Work)")


if __name__ == "__main__":
    unittest.main()
