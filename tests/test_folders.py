"""Chat folders (folders.py) and scripts/merge_agent.py.
Synthetic vault in a temp dir; no model calls."""
import dataclasses
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agents
import folders
import vault
from agents import AGENTS, AgentConfig


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        registry = patch.dict(AGENTS, {
            "body": AgentConfig("body", "Body", "Body", "#6a8f60", "Synthetic test agent"),
            "other": AgentConfig("other", "Other", "Other", "#000000", "Synthetic test agent"),
        }, clear=True)
        registry.start()
        self.addCleanup(registry.stop)
        config = dataclasses.replace(vault.CONFIG, owner_name="")
        for target, name, value in ((vault, "VAULT_ROOT", root), (vault, "AGENTS_ROOT", root / "OpenClave" / "Agents"),
                                    (vault, "CONFIG", config)):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        for agent in ("body", "other"):
            vault.agent_root(agent).mkdir(parents=True)

    def chat(self, cid, agent="body", text="Heel pain after the run"):
        vault.append_turn(agent, cid, "user", text, first_message=text)
        vault.append_turn(agent, cid, "assistant", "Ease off two days.")


class FolderTests(Base):
    def test_folders(self):
        self.assertEqual(folders.list_folders("body"), [])
        folders.create_folder("body", "  Plantar   fasciitis ")
        folders.create_folder("body", "Elbow")
        with self.assertRaises(FileExistsError):
            folders.create_folder("body", "elbow")
        with self.assertRaises(ValueError):
            folders.create_folder("body", " ")
        self.chat("c1")
        self.chat("c2")
        before = vault.conversation_file("body", "c1").stat().st_mtime_ns
        self.assertTrue(folders.set_conversation_folder("body", "c1", "Elbow"))
        self.assertEqual(vault.conversation_file("body", "c1").stat().st_mtime_ns, before, "doesn't jump to the top")
        self.assertEqual(folders.list_folders("body"), [{"name": "Plantar fasciitis", "chats": 0}, {"name": "Elbow", "chats": 1}])
        self.assertEqual({c["id"]: c["folder"] for c in vault.list_conversations("body")}, {"c1": "Elbow", "c2": None})
        self.assertEqual(vault.get_conversation("body", "c1")["folder"], "Elbow")
        # Moving into a new name makes the folder.
        folders.set_conversation_folder("body", "c2", "Hip")
        self.assertEqual([f["name"] for f in folders.list_folders("body")], ["Plantar fasciitis", "Elbow", "Hip"])
        folders.rename_folder("body", "Elbow", "Left elbow")
        self.assertEqual(vault.get_conversation("body", "c1")["folder"], "Left elbow")
        with self.assertRaises(FileExistsError):
            folders.rename_folder("body", "Hip", "left elbow")
        folders.delete_folder("body", "Left elbow")
        self.assertIsNone(vault.get_conversation("body", "c1")["folder"], "the chat stays, unfiled")
        self.assertEqual([f["name"] for f in folders.list_folders("body")], ["Plantar fasciitis", "Hip"])
        self.assertTrue(folders.set_conversation_folder("body", "c2", None))
        self.assertFalse(folders.set_conversation_folder("body", "missing", "Hip"))

    def test_archived_chat(self):
        self.chat("c1")
        vault.archive_conversation("body", "c1")
        self.assertTrue(folders.set_conversation_folder("body", "c1", "Hip"))
        self.assertEqual(vault.list_archived_conversations("body")[0]["folder"], "Hip")
        folders.delete_folder("body", "Hip")
        self.assertIsNone(vault.list_archived_conversations("body")[0]["folder"])

    def test_routes(self):
        from fastapi.testclient import TestClient
        import main
        if not any(getattr(r, "path", "") == "/body/folders" for r in main.app.routes):
            main._register_agent_routes("body")  # main may predate "body"
        c = TestClient(main.app)
        self.assertEqual(c.post("/body/folders", json={"name": "Elbow"}).json(), {"name": "Elbow", "chats": 0})
        self.assertEqual(c.post("/body/folders", json={"name": "Elbow"}).status_code, 409)
        self.chat("c1")
        self.assertEqual(c.patch("/body/conversations/c1", json={"folder": "Elbow"}).json()["folder"], "Elbow")
        self.assertEqual(c.get("/body/folders").json(), [{"name": "Elbow", "chats": 1}])
        self.assertEqual(c.get("/body/conversations").json()[0]["folder"], "Elbow")
        self.assertEqual(c.patch("/body/conversations/c1", json={}).status_code, 400)
        self.assertEqual(c.patch("/body/folders/Elbow", json={"name": "Arm"}).json(), {"name": "Arm"})
        self.assertEqual(c.delete("/body/folders/Arm").status_code, 200)
        self.assertIsNone(c.get("/body/conversations").json()[0]["folder"])
        self.assertEqual(c.patch("/body/conversations/c1", json={"title": "Renamed"}).json()["title"], "Renamed")
        made = c.post("/body/folders/Hip/conversations").json()
        listed = {x["id"]: x for x in c.get("/body/conversations").json()}
        self.assertEqual((listed[made["id"]]["folder"], listed[made["id"]]["title"]), ("Hip", "New chat"))
        vault.append_turn("body", made["id"], "user", "Hip clicks when squatting", first_message="Hip clicks when squatting")
        self.assertEqual(vault.get_conversation("body", made["id"])["title"], "Hip clicks when squatting", "named from the first message")


class MergeAgentTests(Base):
    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location("merge_agent", Path(__file__).resolve().parent.parent / "scripts" / "merge_agent.py")
        self.merge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.merge)
        p = patch.object(self.merge, "backup", lambda s, t: Path("/dev/null"))
        p.start()
        self.addCleanup(p.stop)
        src = vault.agent_root("other")
        (src / "mission.md").write_text("---\nAgent: Other\n---\n# Goals\n\n- Look good\n\n## Domains\n\n- Skin\n")
        (src / "persona.md").write_text("---\nAgent: Other\n---\nBlunt.\n\n## Feedback\n\n- Be shorter.\n")
        (src / "backlog.md").write_text("# Backlog\n\n- [ ] Try retinol\n- [x] Old done thing\n")
        (src / "plan.md").write_text("---\nPhases: []\n---\n# Plan\n\nMinoxidil twice daily.\n")
        (src / "tracking.md").write_text("# Other tracking\n\n## Check-ins\n\n| Date | x |\n|---|---|\n")
        (src / "notes.md").write_text("keep me")
        vault.memory_dir("other").mkdir()
        (vault.memory_dir("other") / "skin.md").write_text("oily")
        (vault.agent_root("body") / "tracking.md").write_text("# Body tracking\n\n## Check-ins\n\n| Date | y |\n|---|---|\n")
        self.chat("o1", "other", "Spots?")
        att = vault.attachments_dir("other", "o1")
        att.mkdir(parents=True)
        (att / "a.bin").write_bytes(b"x")
        self.chat("o2", "other", "Old")
        vault.archive_conversation("other", "o2")

    def test_merge(self):
        dry = self.merge.merge("other", "body", "Skin & style", dry=True, log=lambda *_: None)
        self.assertEqual(dry["chats"], 2)
        self.assertTrue(vault.agent_root("other").exists(), "a dry run changes nothing")

        result = self.merge.merge("other", "body", "Skin & style", log=lambda *_: None)
        self.assertEqual((result["chats"], result["memories"], result["note"]), (2, 1, "Other.md"))
        note = (vault.agent_root("body") / "Other.md").read_text()
        for text in ("- [ ] Try retinol", "Look good", "Be shorter.", "Minoxidil twice daily.", "Other/notes.md"):
            self.assertIn(text, note)
        self.assertNotIn("Old done thing", note)
        self.assertEqual((vault.agent_root("body") / "Other" / "notes.md").read_text(), "keep me")
        self.assertEqual((vault.memory_dir("body") / "skin.md").read_text(), "oily")
        self.assertIn("## Skin & style: Check-ins", (vault.agent_root("body") / "tracking.md").read_text())
        convo = vault.get_conversation("body", "o1")
        self.assertEqual(convo["folder"], "Skin & style")
        self.assertEqual([t["role"] for t in convo["turns"]], ["user", "assistant"], "old turns still parse")
        self.assertTrue((vault.attachments_dir("body", "o1") / "a.bin").exists())
        self.assertEqual(vault.list_archived_conversations("body")[0]["folder"], "Skin & style")
        self.assertEqual(folders.list_folders("body"), [{"name": "Skin & style", "chats": 1}])
        self.assertNotIn("other", AGENTS)
        self.assertTrue((vault.AGENTS_ROOT / "_Retired" / "Other" / "RETIRED.md").exists())
        self.assertEqual(list(vault.AGENTS_ROOT.glob("*/agent.json")), [], "retired folders never load")

    def test_refuses_manager(self):
        AGENTS["other"] = agents._agent_config({"id": "other", "name": "Other", "color": "#000", "tagline": "t", "roles": ["manager"]}, "Other")
        with self.assertRaises(SystemExit):
            self.merge.merge("other", "body", "X", dry=True, log=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
