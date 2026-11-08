"""Group chat channels the owner makes (channels.py, 2026-11-05). Temp state, no model calls."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import channels
import chat_jobs
import main
import room
import vault


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        (base / "Agents").mkdir()
        for p in (patch.object(vault, "AGENTS_ROOT", base / "Agents"),
                  patch.object(room, "ROOM_FILE", base / "room.jsonl"),
                  patch.object(room, "CONVERSATIONS_FILE", base / "room.json"),
                  patch.object(channels, "STATE_FILE", base / "channels.json")):
            p.start()
            self.addCleanup(p.stop)
        for agent in ("rick", "stash", "r2"):
            vault.agent_root(agent).mkdir()
        self.asked = []
        submit = patch.object(chat_jobs, "submit", side_effect=lambda a, r, c, m, att=None, *, extra=None, schedule=True:
                              (self.asked.append({"agent_id": a, "conversation_id": c, **(extra or {})}), ({}, True))[1])
        submit.start()
        self.addCleanup(submit.stop)
        self.client = TestClient(main.app)

    def test_create_post_tag_in_and_delete(self):
        ch = self.client.post("/api/channels", json={"name": "# Garage ", "members": ["r2", "nobody"]}).json()
        self.assertEqual((ch["name"], ch["members"]), ("garage", ["r2"]))
        self.assertEqual(self.client.post("/api/channels", json={"name": "garage"}).status_code, 400)
        self.assertEqual(self.client.post("/api/channels", json={"name": "everyone"}).status_code, 400)
        # @all is the channel's members only
        self.client.post("/api/room", json={"text": "@all door open?", "clave": ch["id"]})
        self.assertEqual([a["agent_id"] for a in self.asked], ["r2"])
        convo = vault.get_conversation("r2", self.asked[0]["conversation_id"])
        self.assertEqual(convo["title"], "Group chat · #garage")
        self.assertIn("#garage channel", self.asked[0]["context_note"])
        # Tagging an outsider brings them in
        posted = self.client.post("/api/room", json={"text": "@stash do we have hinges?", "clave": ch["id"]}).json()
        self.assertEqual(posted["joined"], ["stash"])
        self.assertEqual(self.client.get(f"/api/room?clave={ch['id']}").json()["members"], ["r2", "stash"])
        # An agent tagging an outsider does not add them
        room.on_done("stash", {"request_id": "x", "status": "completed", "room": {"seq": posted["seq"], "hops": 0, "clave": ch["id"]}},
                     "@rick knows")
        self.assertNotIn("rick", channels.members(ch["id"]))
        # Everyone room unaffected
        self.assertEqual(self.client.get("/api/room").json()["messages"], [])
        renamed = self.client.patch(f"/api/channels/{ch['id']}", json={"name": "shed", "members": ["r2"]}).json()
        self.assertEqual((renamed["name"], renamed["members"]), ("shed", ["r2"]))
        self.assertEqual(self.client.delete(f"/api/channels/{ch['id']}").status_code, 200)
        self.assertEqual(self.client.get("/api/channels").json(), [])
        self.assertEqual(self.client.get(f"/api/room?clave={ch['id']}").status_code, 404)
        self.assertTrue(room._file(ch["id"]).with_suffix(".deleted").exists(), "messages kept, out of the list")


if __name__ == "__main__":
    unittest.main()
