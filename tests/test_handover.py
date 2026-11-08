"""Handover: the conversation and the chosen files go to another agent, which
replies "Handover registered"; nothing moves if a file would clash."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import handover
import main
import vault

CID = "20260929-120000-abcdef12"


class HandoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "Agents"
        root.mkdir()
        patcher = patch.object(vault, "AGENTS_ROOT", root)
        patcher.start()
        self.addCleanup(patcher.stop)
        for agent in ("rick", "stash"):
            vault.agent_root(agent).mkdir()
        rick = vault.agent_root("rick")
        (rick / "purchases.md").write_text("| Item | Price |\n| cable | 99 |\n")
        (rick / "business-ideas.md").write_text("ideas")
        (rick / "backlog.md").write_text("- [ ] core file")
        (rick / "inbox").mkdir()
        (rick / "inbox" / "item.json").write_text("{}")
        vault.append_turn("rick", CID, "user", "Log this cable, 99 kr", first_message="Log this cable")
        vault.append_turn("rick", CID, "assistant", "Added to your purchase log.")

    def test_suggests_files_the_chat_mentions_and_never_core_files(self):
        files = handover.related_files("rick", CID)
        self.assertEqual(files[0], {"path": "purchases.md", "suggested": True})
        self.assertIn({"path": "business-ideas.md", "suggested": False}, files)
        self.assertNotIn("backlog.md", [f["path"] for f in files])
        self.assertNotIn("inbox/item.json", [f["path"] for f in files])
        self.assertIsNone(handover.related_files("rick", "20260101-000000-00000000"))

    def test_moves_conversation_and_files_and_target_registers_it(self):
        result = handover.handover("rick", "stash", CID, ["purchases.md"])
        self.assertEqual(result["moved"], ["purchases.md"])
        self.assertIsNone(vault.get_conversation("rick", CID))
        self.assertFalse((vault.agent_root("rick") / "purchases.md").exists())
        self.assertIn("cable | 99", (vault.agent_root("stash") / "purchases.md").read_text())
        self.assertTrue((vault.agent_root("rick") / "business-ideas.md").exists())
        turns = vault.get_conversation("stash", CID)["turns"]
        self.assertEqual(turns[1]["content"], "Added to your purchase log.")  # history kept as it was
        self.assertTrue(turns[-1]["content"].startswith("Handover registered."))
        self.assertIn("from Rick", turns[-1]["content"])
        self.assertIn("`purchases.md`", turns[-1]["content"])

    def test_file_clash_stops_everything_before_anything_moves(self):
        (vault.agent_root("stash") / "purchases.md").write_text("Stash's own")
        with self.assertRaises(FileExistsError):
            handover.handover("rick", "stash", CID, ["purchases.md"])
        self.assertIsNotNone(vault.get_conversation("rick", CID))
        self.assertTrue((vault.agent_root("rick") / "purchases.md").exists())

    def test_core_files_and_bad_targets_are_refused(self):
        with self.assertRaises(ValueError):
            handover.handover("rick", "stash", CID, ["backlog.md"])
        with self.assertRaises(ValueError):
            handover.handover("rick", "stash", CID, ["inbox/item.json"])
        with self.assertRaises(ValueError):
            handover.handover("rick", "rick", CID, [])
        self.assertIsNotNone(vault.get_conversation("rick", CID))

    def test_api(self):
        client = TestClient(main.app)
        files = client.get(f"/rick/conversations/{CID}/handover-files")
        self.assertEqual(files.status_code, 200)
        self.assertTrue(files.json()[0]["suggested"])
        res = client.post("/api/conversations/handover", json={
            "source_agent": "rick", "target_agent": "stash", "conversation_id": CID, "files": ["purchases.md"]})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["target_agent"], "stash")
        again = client.post("/api/conversations/handover", json={
            "source_agent": "rick", "target_agent": "stash", "conversation_id": CID, "files": []})
        self.assertEqual(again.status_code, 404)


if __name__ == "__main__":
    unittest.main()
