"""Claves (claves.py, 2026-11-04): groups of agents with their own group chat.
Synthetic folders in a temp dir; no model calls."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import agent_factory
import agent_tools
import agents
import chat_jobs
import claves
import llm_models
import main
import room
import service_settings
import vault


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / "Agents").mkdir()
        for p in (patch.object(vault, "AGENTS_ROOT", self.base / "Agents"),
                  patch.object(claves, "root", lambda: self.base / "Claves"),
                  patch.object(service_settings, "SETTINGS_FILE", self.base / "settings.json"),
                  patch.object(room, "ROOM_FILE", self.base / "room.jsonl"),
                  patch.object(room, "CONVERSATIONS_FILE", self.base / "room.json")):
            p.start()
            self.addCleanup(p.stop)
        for agent in ("rick", "stash", "r2"):
            vault.agent_root(agent).mkdir()
        self.client = TestClient(main.app)


class ClaveTests(Base):
    def test_off_by_default_and_turning_on_makes_personal_with_everyone(self):
        self.assertFalse(self.client.get("/api/claves").json()["enabled"])
        data = self.client.post("/api/claves/enabled", json={"enabled": True}).json()
        self.assertTrue(data["enabled"])
        self.assertEqual([c["name"] for c in data["claves"]], ["Personal"])
        self.assertEqual(set(data["claves"][0]["members"]), set(agents.AGENTS))
        # Turning it off and on again doesn't make a second one.
        self.client.post("/api/claves/enabled", json={"enabled": False})
        data = self.client.post("/api/claves/enabled", json={"enabled": True}).json()
        self.assertEqual(len(data["claves"]), 1)

    def test_create_rename_lead_and_files(self):
        made = claves.create("RnD", members=["r2", "stash"])
        self.assertEqual(made["id"], "rnd")
        text = (self.base / "Claves" / "RnD" / "clave.md").read_text()
        self.assertIn("Members:\n- r2\n- stash", text)
        self.assertTrue((self.base / "Claves" / "RnD" / "backlog.md").exists())
        with self.assertRaises(ValueError):
            claves.create("rnd")
        with self.assertRaises(ValueError):
            claves.update("rnd", lead="rick")  # not a member
        claves.update("rnd", name="Workshop", lead="r2")
        c = claves.get("rnd").as_dict()
        self.assertEqual((c["name"], c["lead"], c["folder"]), ("Workshop", "r2", "RnD"))
        claves.update("rnd", members=["stash"])
        self.assertIsNone(claves.get("rnd").as_dict()["lead"], "lead left with its membership")

    def test_one_clave_per_agent_but_the_manager_is_everywhere(self):
        claves.create("Personal", members=["rick", "stash", "r2"])
        claves.create("RnD", members=["stash"])
        self.assertEqual(claves.clave_of("stash"), "rnd", "creating RnD with Stash moved it")
        self.assertEqual(claves.get("personal").members, ["rick", "r2"])
        claves.place("r2", "rnd", index=0)
        self.assertEqual(claves.get("rnd").members, ["r2", "stash"])
        self.assertEqual(claves.get("personal").members, ["rick"], "a drag moves")
        self.assertEqual(claves.members("rnd"), ["r2", "stash", "rick"], "the manager is in every room")
        claves.place("rick", "rnd", lead=True)
        self.assertEqual((claves.get("rnd").lead, claves.clave_of("rick")), ("rick", None))
        self.assertIn("rick", claves.get("personal").members, "the manager stays where it was too")
        self.assertEqual(claves.set_agent_clave("r2", "personal"), "personal")
        self.assertEqual(claves.set_agent_clave("r2", None), None)
        with self.assertRaises(ValueError):
            claves.set_agent_clave("rick", "rnd")
        claves.remove("rick", "rnd")
        self.assertIsNone(claves.get("rnd").lead)
        self.client.delete("/api/claves/rnd")
        self.assertEqual([c.id for c in claves.list_claves()], ["personal"])
        self.assertTrue((self.base / "Claves" / "_Archived" / "RnD" / "clave.md").exists(), "archived, not deleted")

    def test_active_clave_limits_what_shows(self):
        claves.create("Personal", members=["rick", "stash"])
        claves.create("RnD", members=["r2"])
        service_settings.update({"multiple_claves": True})
        self.assertEqual(claves.active(), "personal")
        claves.set_active("rnd")
        self.assertEqual(claves.visible(["rick", "stash", "r2"]), ["rick", "r2"])


class NewAgentInClaveTests(Base):
    def test_new_agent_from_a_clave_box_joins_it(self):
        claves.create("RnD")
        with patch.object(agent_factory, "create_agent", return_value={"id": "r2", "conversation_id": "c"}), \
                patch.object(main, "_register_agent_routes"):
            self.client.post("/api/agents", json={"name": "R2", "purpose": "x", "model_id": "m", "clave": "rnd"})
            self.client.post("/api/agents", json={"name": "R2", "purpose": "x", "model_id": "m", "clave": "gone"})
        self.assertEqual(claves.get("rnd").members, ["r2"])


class ClaveRoomTests(Base):
    def setUp(self):
        super().setUp()
        claves.create("RnD", members=["stash", "r2"])
        self.asked = []
        submit = patch.object(chat_jobs, "submit", side_effect=self.fake_submit)
        submit.start()
        self.addCleanup(submit.stop)

    def fake_submit(self, agent_id, request_id, conversation_id, message, attachments=None, *, extra=None, schedule=True):
        self.asked.append({"agent_id": agent_id, "request_id": request_id, "conversation_id": conversation_id,
                           "message": message, **(extra or {})})
        return {}, True

    def test_clave_room_is_its_own_and_only_members_answer(self):
        posted = self.client.post("/api/room", json={"text": "@all hi @spice", "clave": "rnd"}).json()
        self.assertEqual(posted["asked"], ["stash", "r2", "rick"], "@all = the clave's members and the manager; Spice isn't in it")
        job = self.asked[0]
        self.assertEqual(job["room"]["clave"], "rnd")
        self.assertIn("RnD clave", job["context_note"])
        convo = vault.get_conversation("stash", job["conversation_id"])
        self.assertEqual((convo["title"], convo["folder"]), ("Group chat · RnD", "RnD"))
        room.on_done("stash", {**job, "status": "completed"}, "Hello from the bench")
        texts = [m["text"] for m in self.client.get("/api/room?clave=rnd").json()["messages"]]
        self.assertEqual(texts, ["@all hi @spice", "Hello from the bench"])
        self.assertEqual(self.client.get("/api/room").json()["messages"], [], "everyone's room is separate")
        self.assertEqual(self.client.get("/api/room?clave=nope").status_code, 404)

    def test_same_agent_has_one_group_chat_per_room(self):
        self.client.post("/api/room", json={"text": "@stash a", "clave": "rnd"})
        self.client.post("/api/room", json={"text": "@stash b"})
        self.client.post("/api/room", json={"text": "@stash c", "clave": "rnd"})
        ids = [a["conversation_id"] for a in self.asked]
        self.assertEqual(ids[0], ids[2])
        self.assertNotEqual(ids[0], ids[1])


class CloneTests(Base):
    def setUp(self):
        super().setUp()
        root = self.base / "Agents"
        src = root / "Stash"
        (src / "memory").mkdir(parents=True)
        (src / "conversations").mkdir()
        (src / "skills" / "count").mkdir(parents=True)
        (src / "mission.md").write_text("---\nAgent: Stash\n---\n\nKeep the inventory.\n")
        (src / "persona.md").write_text("Tidy.\n")
        (src / "skills" / "count" / "SKILL.md").write_text("Count things.\n")
        (src / "memory" / "facts.md").write_text("Shelf B holds cables.\n")
        (src / "conversations" / "c1.md").write_text("old chat\n")
        (src / "notes.md").write_text("a file\n")
        (src / "agent.json").write_text(json.dumps({"id": "stash", "name": "Stash", "color": "#123456",
                                                    "tagline": "Inventory", "roles": ["lead-ish"]}))
        cfg = agents._agent_config(json.loads((src / "agent.json").read_text()), "Stash")
        for p in (patch.object(agents, "AGENTS_ROOT", root), patch.dict(agents.AGENTS, {"stash": cfg}),
                  patch.object(agent_factory, "AVATAR_DIR", self.base / "avatars"),
                  patch.object(llm_models, "set_selected_model"),
                  patch.object(agent_tools, "set_optional")):
            p.start()
            self.addCleanup(p.stop)
        claves.create("Personal", members=["stash"])
        claves.create("RnD")

    def test_clone_copies_setup_not_memory_and_joins_the_clave(self):
        made = agent_factory.clone_agent("stash", "rnd")
        self.assertEqual((made["id"], made["name"]), ("stash-rnd", "Stash"))
        target = self.base / "Agents" / "Stash (RnD)"
        self.assertEqual((target / "persona.md").read_text(), "Tidy.\n")
        self.assertEqual((target / "skills" / "count" / "SKILL.md").read_text(), "Count things.\n")
        self.assertEqual(list((target / "memory").iterdir()), [], "empty memory")
        self.assertEqual(list((target / "conversations").iterdir()), [], "no chats")
        self.assertFalse((target / "notes.md").exists(), "no files")
        data = json.loads((target / "agent.json").read_text())
        self.assertEqual((data["id"], data["name"], data["cloned_from"]), ("stash-rnd", "Stash", "stash"))
        self.assertEqual(claves.clave_of("stash-rnd"), "rnd")
        self.assertEqual(claves.clave_of("stash"), "personal", "the original stays")
        with self.assertRaises(ValueError):
            agent_factory.clone_agent("stash", "rnd")  # RnD has a Stash already

    def test_clones_share_a_name_and_messages_find_the_one_in_the_senders_clave(self):
        import agent_messages
        agent_factory.clone_agent("stash", "rnd")
        r2 = agents.AgentConfig("r2", "R2", "R2", "#000000", "x")
        with patch.dict(agents.AGENTS, {"r2": r2}):
            claves.place("r2", "rnd")
            self.assertEqual(agent_messages.resolve("Stash", "r2"), "stash-rnd")
            self.assertEqual(agent_messages.resolve("stash"), "stash", "an id is exact")


class ClaveBacklogTests(Base):
    def test_lead_owns_the_backlog_members_add_and_everyone_is_told(self):
        import agent_tools
        self.assertNotIn("clave_backlog", agent_tools.allowed("stash"), "claves off: no tool")
        self.client.post("/api/claves/enabled", json={"enabled": True})
        rnd = claves.create("RnD", members=["stash", "r2"], lead="r2")
        self.assertIn("clave_backlog", agent_tools.allowed("stash"))
        cid = rnd["id"]
        self.assertEqual(claves.backlog_tool("stash", "add", "Order hinges", cid), "Success!")
        self.assertIn("refused", claves.backlog_tool("stash", "check", "hinges", cid))
        self.assertEqual(claves.backlog_tool("r2", "check", "hinges", cid), "Success!")
        self.assertIn("- [x] Order hinges", claves.backlog_tool("stash", "read", "", cid))
        self.assertIn("You lead the RnD clave", claves.prompt_line("r2"))
        self.assertIn("led by", claves.prompt_line("stash"))
        self.assertIn("clave_backlog", {t["function"]["name"] for t in __import__("llm_client").TOOLS})


if __name__ == "__main__":
    unittest.main()
