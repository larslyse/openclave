"""Agents messaging each other, and the group chat room."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import agent_messages
import chat_jobs
import chat_service
import ha_guard
import main
import room
import vault


class AgentMessageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "Agents"
        root.mkdir()
        for p in (patch.object(vault, "AGENTS_ROOT", root),
                  patch.object(chat_jobs, "_kick_dir", lambda: Path(self.temp.name))):
            p.start()
            self.addCleanup(p.stop)
        for agent in ("rick", "stash", "r2"):
            vault.agent_root(agent).mkdir()

    def queued(self, agent_id):
        return [json.loads(p.read_text()) for p in chat_jobs._requests_dir(agent_id).glob("*.json")]

    def test_message_lands_in_a_from_conversation_created_once(self):
        first = agent_messages.send("rick", "Stash", "Log a cable, 99 kr")
        second = agent_messages.send("rick", "stash", "And a plug, 49 kr")
        self.assertEqual(first["conversation_id"], second["conversation_id"])
        convo = vault.get_conversation("stash", first["conversation_id"])
        self.assertEqual(convo["title"], "From Rick")
        records = self.queued("stash")
        self.assertEqual({r["sender"] for r in records}, {"rick"})
        self.assertEqual({r["status"] for r in records}, {"queued"})
        self.assertTrue((Path(self.temp.name) / "stash").exists())  # service kicked
        self.assertEqual(agent_messages.threads()[0]["from_agent"], "rick")

    def test_sender_is_recorded_on_the_turn_and_shown_to_the_model(self):
        cid = agent_messages.conversation_for("stash", "rick")
        vault.append_turn("stash", cid, "user", "Log a cable", sender="rick")
        turn = vault.get_conversation("stash", cid)["turns"][0]
        self.assertEqual((turn["from_agent"], turn["content"]), ("rick", "Log a cable"))
        self.assertTrue(chat_service._history_content(turn).startswith("[Message from Rick]"))
        self.assertIn("not ", chat_service._message_header("rick"))

    def test_refuses_unknown_self_empty_and_runaway_pairs(self):
        self.assertIn("refused", agent_messages.tool_reply("rick", "nobody", "hi"))
        self.assertIn("refused", agent_messages.tool_reply("rick", "rick", "hi"))
        self.assertIn("refused", agent_messages.tool_reply("rick", "stash", " "))
        for i in range(agent_messages.PAIR_LIMIT):
            self.assertIn("Delivered", agent_messages.tool_reply("rick", "stash", f"msg {i}"))
        self.assertIn("refused", agent_messages.tool_reply("rick", "stash", "one more"))
        self.assertIn("Delivered", agent_messages.tool_reply("stash", "rick", "other direction is its own pair"))

    def test_agent_yes_never_approves_home_assistant(self):
        cid = vault.new_conversation_id()
        with patch.object(ha_guard, "on_owner_message", return_value=(0, "")) as approve, \
                patch.object(chat_service, "_chat_checked", return_value="ok"), \
                patch.object(chat_service.llm_client, "maybe_compact_conversation"), \
                patch.object(chat_service.retrieval, "build_context", return_value=""), \
                patch.object(chat_service, "_system_prompt", return_value=""):
            chat_service.run_chat_locked("r2", cid, "yes", sender="rick")
            approve.assert_not_called()
            chat_service.run_chat_locked("r2", cid, "yes")
            approve.assert_called_once()


class RoomTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        (base / "Agents").mkdir()
        for p in (patch.object(vault, "AGENTS_ROOT", base / "Agents"),
                  patch.object(room, "ROOM_FILE", base / "room.jsonl"),
                  patch.object(room, "CONVERSATIONS_FILE", base / "room.json")):
            p.start()
            self.addCleanup(p.stop)
        for agent in ("rick", "stash", "r2"):
            vault.agent_root(agent).mkdir()
        self.asked = []
        submit = patch.object(chat_jobs, "submit", side_effect=self.fake_submit)
        submit.start()
        self.addCleanup(submit.stop)
        self.client = TestClient(main.app)

    def fake_submit(self, agent_id, request_id, conversation_id, message, attachments=None, *, extra=None, schedule=True):
        self.asked.append({"agent_id": agent_id, "request_id": request_id, "conversation_id": conversation_id,
                           "message": message, **(extra or {})})
        return {}, True

    def finish(self, job, reply):
        room.on_done(job["agent_id"], {**job, "status": "completed"}, reply)

    def test_mentions_pick_responders_and_thread_continues(self):
        posted = self.client.post("/api/room", json={"text": "@Stash what did I spend? @rick too"}).json()
        self.assertEqual(posted["asked"], ["stash", "rick"])
        self.assertIsNone(self.asked[0]["sender"])
        self.assertIn("Group chat", self.asked[0]["context_note"])
        convo = vault.get_conversation("stash", self.asked[0]["conversation_id"])
        self.assertEqual(convo["title"], "Group chat")
        self.finish(self.asked[0], "About 500 kr.")
        self.asked.clear()
        follow = self.client.post("/api/room", json={"text": "And last month?"}).json()
        self.assertEqual(follow["asked"], ["stash"])  # only Stash had answered
        self.assertIn("Stash: About 500 kr.", self.asked[0]["context_note"])
        texts = [m["text"] for m in self.client.get("/api/room").json()["messages"]]
        self.assertEqual(texts[1], "About 500 kr.")

    def test_no_mention_and_no_thread_asks_nobody(self):
        self.assertEqual(self.client.post("/api/room", json={"text": "hello"}).json()["asked"], [])
        self.assertEqual(self.asked, [])

    def test_everyone_and_agent_mentions_are_capped(self):
        self.client.post("/api/room", json={"text": "@all hi"})
        self.assertEqual({a["agent_id"] for a in self.asked}, set(main.AGENTS))
        self.asked.clear()
        self.client.post("/api/room", json={"text": "@rick start"})
        hops = 0
        while self.asked:
            job = self.asked.pop(0)
            other = "stash" if job["agent_id"] == "rick" else "rick"
            self.finish(job, f"@{other} your turn")
            hops += 1
        self.assertEqual(hops, room.MAX_HOPS)
        last = self.client.get("/api/room").json()["messages"][-1]
        self.assertEqual(last["asked"], [])


if __name__ == "__main__":
    unittest.main()
