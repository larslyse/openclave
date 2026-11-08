"""Purge (purge.py, 2026-11-05): preview, purge, undo. Temp vault; the model is faked."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
import purge
import vault


class PurgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p = patch.object(vault, "AGENTS_ROOT", Path(self.temp.name) / "Agents")
        p.start()
        self.addCleanup(p.stop)
        vault.agent_root("stash").mkdir(parents=True)
        self.cid = vault.new_conversation_id()
        vault.append_turn("stash", self.cid, "user", "Where did I put the drill bits? " * 20, first_message="Drill bits")
        vault.append_turn("stash", self.cid, "assistant", "Shelf B, blue box. " * 30)
        (vault.agent_root("stash") / "notes.md").write_text(f"See chat {self.cid}\n")
        self.client = TestClient(main.app)

    def test_preview_purge_and_undo(self):
        original = vault.conversation_file("stash", self.cid).read_text()
        with patch.object(purge, "_generate", return_value=("- Drill bits: shelf B, blue box", {})), \
             patch.object(purge.recaps, "available", return_value=True):
            info = self.client.post(f"/stash/conversations/{self.cid}/purge/preview").json()
        self.assertEqual(info["summary"], "- Drill bits: shelf B, blue box")
        self.assertEqual(info["mentioned_in"], ["notes.md"])
        self.assertEqual(info["turns"], 2)
        self.assertEqual(vault.conversation_file("stash", self.cid).read_text(), original, "preview changes nothing")
        self.client.post(f"/stash/conversations/{self.cid}/purge", json={"summary": info["summary"]})
        convo = self.client.get(f"/stash/conversations/{self.cid}").json()
        self.assertEqual((convo["turns"], convo["summary"], convo["purged"], convo["title"]),
                         ([], "- Drill bits: shelf B, blue box", True, "Drill bits"))
        self.client.post(f"/stash/conversations/{self.cid}/purge/undo")
        self.assertEqual(vault.conversation_file("stash", self.cid).read_text(), original, "undo restores it exactly")
        self.assertEqual(self.client.post(f"/stash/conversations/{self.cid}/purge/undo").status_code, 400)

    def test_nothing_worth_keeping(self):
        with patch.object(purge, "_generate", return_value=("NOTHING", {})), patch.object(purge.recaps, "available", return_value=True):
            self.assertEqual(purge.preview("stash", self.cid)["summary"], "")
        purge.apply("stash", self.cid, "")
        self.assertIn("nothing in it was worth keeping", vault.get_conversation("stash", self.cid)["summary"])


if __name__ == "__main__":
    unittest.main()
