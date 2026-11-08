import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agents
import vault

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "Agents"


class LatestActivityTests(unittest.TestCase):
    """Mobile overview's unread tag and recency sort, against a synthetic agent."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        root = tmp / "Agents"
        root.mkdir()
        shutil.copytree(FIXTURE_ROOT / "Spice", root / "Spice")
        for patcher in (patch.object(agents, "AGENTS_ROOT", root), patch.object(vault, "AGENTS_ROOT", root)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_no_conversations(self):
        self.assertEqual(vault.latest_activity("spice"), {"last_activity_ms": None, "last_is_reply": False, "last_conversation_id": None})

    def test_reply_then_user_turn_in_newest_conversation(self):
        old, new = "20260101-000000-aaaaaaaa", "20260102-000000-bbbbbbbb"
        vault.append_turn("spice", old, "user", "hi")
        vault.append_turn("spice", old, "assistant", "hello")
        vault.append_turn("spice", new, "user", "what music?")
        f_old = vault.conversation_file("spice", old)
        os.utime(f_old, (1, 1))  # make sure "new" is the newest
        state = vault.latest_activity("spice")
        self.assertFalse(state["last_is_reply"])  # newest ends with the owner's turn
        vault.append_turn("spice", new, "assistant", "Trip-hop.")
        state = vault.latest_activity("spice")
        self.assertTrue(state["last_is_reply"])
        self.assertEqual(state["last_activity_ms"], vault.conversation_file("spice", new).stat().st_mtime_ns // 1_000_000)
        self.assertEqual(state["last_conversation_id"], new)
        listed = {c["id"]: c for c in vault.list_conversations("spice")}
        self.assertTrue(listed[new]["last_is_reply"])
        self.assertTrue(listed[old]["last_is_reply"])
        self.assertEqual(listed[new]["updated_ms"], state["last_activity_ms"])


if __name__ == "__main__":
    unittest.main()
