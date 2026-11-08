"""Control Panel → Sorting (2026-11-05): conversation and agent-tab order."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import agent_order
import main
import service_settings
import vault


class SortingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        for p in (patch.object(vault, "AGENTS_ROOT", base / "Agents"),
                  patch.object(service_settings, "SETTINGS_FILE", base / "settings.json"),
                  patch.object(agent_order, "ORDER_FILE", base / "order.json")):
            p.start()
            self.addCleanup(p.stop)
        for agent in ("rick", "stash", "r2"):
            vault.agent_root(agent).mkdir(parents=True)
        self.client = TestClient(main.app)

    def chat(self, agent, cid, title, age_s):
        vault.append_turn(agent, cid, "user", title, first_message=title)
        t = time.time() - age_s
        os.utime(vault.conversation_file(agent, cid), (t, t))

    def test_conversations_newest_or_alphabetical(self):
        self.chat("stash", "a", "Zebra stripes", 10)
        self.chat("stash", "b", "apple pie", 100)
        titles = lambda: [c["title"] for c in self.client.get("/stash/conversations").json()]
        self.assertEqual(titles(), ["Zebra stripes", "apple pie"])
        self.client.post("/api/settings", json={"sort_conversations": "alphabetical"})
        self.assertEqual(titles(), ["apple pie", "Zebra stripes"])

    def test_agent_tabs_manual_or_latest_message_first(self):
        agent_order.set_order(["rick", "stash", "r2"])
        self.chat("r2", "x", "lights", 5)
        self.chat("stash", "y", "boxes", 50)
        ids = lambda: [t["id"] for t in self.client.get("/rick/agent-tabs").json() if t["id"] in {"rick", "stash", "r2"}]
        self.assertEqual(ids(), ["rick", "stash", "r2"])
        self.client.post("/api/settings", json={"sort_agent_tabs": "recent"})
        self.assertEqual(ids(), ["r2", "stash", "rick"])
        self.assertEqual(self.client.post("/api/settings", json={"sort_agent_tabs": "random"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
