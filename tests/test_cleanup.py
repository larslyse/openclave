"""The maintainer's weekly cleanup: suggestions only, and
nothing changes until the owner applies one."""
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import chat_jobs
import cleanup
import vault
from agents import AGENTS, AgentConfig

DAY = 86400


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        greg = AgentConfig("greg", "Greg", "Greg", "#6a8f60", "Synthetic test agent")
        registry = patch.dict(AGENTS, {"greg": greg})
        registry.start()
        self.addCleanup(registry.stop)
        root = Path(self.temp.name).resolve()
        for target, name, value in (
            (vault, "VAULT_ROOT", root),
            (vault, "AGENTS_ROOT", root / "OpenClave" / "Agents"),
            (cleanup, "STATE_FILE", root / "cleanup_state.json"),
            (cleanup, "AGENTS", {a: AGENTS[a] for a in ("greg", "rick")}),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        for agent in ("greg", "rick"):
            vault.agent_root(agent).mkdir(parents=True)
        self.now = time.time()

    def convo(self, cid, exchanges, idle_days, message="Heat pump integration with the garage unit, details", agent="greg",
              reply="A long and useful answer. " * 20):
        for i in range(exchanges):
            text = message if i == 0 else f"more {i}"
            vault.append_turn(agent, cid, "user", text, first_message=text)
            vault.append_turn(agent, cid, "assistant", reply)
        f = vault.conversation_file(agent, cid)
        t = self.now - idle_days * DAY
        os.utime(f, (t, t))

    def kinds(self, agent="greg", name=None):
        s, notes = cleanup.suggestions_for(agent, datetime.fromtimestamp(self.now), name)
        return {x["conversation_id"]: x["kind"] for x in s}, s, notes

    def test_rules(self):
        self.convo("stale", 3, 40)
        self.convo("fresh", 3, 1)
        self.convo("single", 1, 20)                      # real exchange: archive, not delete
        self.convo("tiny", 1, 9, message="hi", reply="Hi! What's up?")           # trivial: delete
        vault.append_turn("greg", "noreply", "user", "did this ever get an answer?", first_message="x")
        os.utime(vault.conversation_file("greg", "noreply"), (self.now - 8 * DAY,) * 2)
        self.convo("recent-single", 1, 3)                # too new to suggest anything
        kinds, _, _ = self.kinds()
        self.assertEqual(kinds, {"stale": "archive", "single": "archive", "tiny": "delete", "noreply": "delete"})

    def test_finished_onboarding_chat_is_offered_for_deletion(self):
        import json
        self.convo("onb", 6, 2, message="Onboarding answers about what Greg should do in the garden")
        agent_file = vault.agent_root("greg") / "agent.json"
        agent_file.write_text(json.dumps({"id": "greg", "onboarding": True, "onboarding_conversation": "onb"}))
        self.assertNotIn("onb", self.kinds()[0])  # still onboarding
        agent_file.write_text(json.dumps({"id": "greg", "onboarding": False, "onboarding_conversation": "onb"}))
        kinds, s, _ = self.kinds()
        self.assertEqual(kinds["onb"], "delete")
        self.assertIn("onboarding is finished", s[0]["reason"])

    def test_too_many_active(self):
        for i in range(8):
            self.convo(f"c{i}", 3, i * 3)  # c0 newest
        kinds, s, notes = self.kinds()
        self.assertEqual(sorted(kinds), ["c5", "c6", "c7"], "the oldest idle ones beyond five")
        self.assertIn("8 active conversations", s[0]["reason"])
        self.assertEqual(notes, [{"agent_id": "greg", "kind": "busy", "text": "8 active conversations"}])

    def test_unclear_names_get_a_suggestion(self):
        self.convo("unnamed", 3, 2)
        self.convo("named", 3, 2)
        vault.rename_conversation("greg", "named", "Heat pump")
        kinds, s, _ = self.kinds(name=lambda u, r: "Heat pump integration")
        self.assertEqual(kinds, {"unnamed": "rename"})
        self.assertEqual(s[0]["suggested_title"], "Heat pump integration")
        self.assertEqual(self.kinds(name=None)[0], {}, "no name, no suggestion")

    def test_dormant_and_service_conversations(self):
        self.convo("old", 3, 45)
        vault.append_turn("greg", "reminders", "assistant", "Reminder!", first_message="Reminders")
        os.utime(vault.conversation_file("greg", "reminders"), (self.now - 90 * DAY,) * 2)
        kinds, _, notes = self.kinds()
        self.assertEqual(kinds, {"old": "archive"}, "never the Reminders conversation")
        self.assertEqual(notes[0]["kind"], "dormant")
        self.assertEqual(self.kinds("rick")[2], [{"agent_id": "rick", "kind": "dormant", "text": "no conversations yet"}])

    def test_apply_archive_restore_delete_rename_and_keep(self):
        self.convo("stale", 3, 40)
        self.convo("tiny", 1, 9, message="hi", reply="Hi! What's up?")
        self.convo("unnamed", 3, 2)
        self.convo("other", 3, 40)
        state = cleanup.generate(name=lambda u, r: "Heat pump integration")
        ids = {s["conversation_id"]: s["id"] for s in state["suggestions"]}
        self.assertEqual(len(ids), 4)
        # Nothing has changed yet.
        self.assertEqual(len(vault.list_conversations("greg")), 4)

        cleanup.apply(ids["stale"])
        self.assertNotIn("stale", [c["id"] for c in vault.list_conversations("greg")])
        self.assertEqual([c["id"] for c in cleanup.report()["archived"]], ["stale"])
        self.assertTrue(vault.unarchive_conversation("greg", "stale"))
        self.assertIn("stale", [c["id"] for c in vault.list_conversations("greg")])

        cleanup.apply(ids["tiny"])
        self.assertIsNone(vault.get_conversation("greg", "tiny"))

        cleanup.apply(ids["unnamed"], title="  Heat pump  ")
        self.assertEqual(vault.get_conversation("greg", "unnamed")["title"], "Heat pump")

        self.assertTrue(cleanup.dismiss(ids["other"]))
        with self.assertRaises(LookupError):
            cleanup.apply(ids["other"])
        again = cleanup.generate(name=lambda u, r: None)
        self.assertNotIn(ids["other"], [s["id"] for s in again["suggestions"]], "Keep sticks")
        self.assertIn(ids["stale"], [s["id"] for s in again["suggestions"]], "restored: suggested again")

    def test_rename_never_overwrites_a_name_given_since(self):
        self.convo("unnamed", 3, 2)
        state = cleanup.generate(name=lambda u, r: "Heat pump integration")
        vault.rename_conversation("greg", "unnamed", "Mine")
        result = cleanup.apply(state["suggestions"][0]["id"])
        self.assertFalse(result["done"])
        self.assertEqual(vault.get_conversation("greg", "unnamed")["title"], "Mine")

    def test_archive_waits_for_running_messages(self):
        self.convo("stale", 3, 40)
        state = cleanup.generate(name=lambda u, r: None)
        with patch.object(chat_jobs, "archive_conversation", side_effect=chat_jobs.AgentBusyError("busy")):
            with self.assertRaises(chat_jobs.AgentBusyError):
                cleanup.apply(state["suggestions"][0]["id"])
        self.assertEqual(len(cleanup.report()["suggestions"]), 1, "still there to retry")

    def test_weekly_run_posts_ricks_summary_and_pushes(self):
        self.convo("stale", 3, 40)
        with patch("web_push.send_all", return_value=1) as push, \
             patch.object(cleanup, "_model_name", lambda u, r: None):
            result = cleanup.run_weekly(datetime(2026, 10, 19, 9, 5))
        self.assertEqual(result["suggestions"], 1)
        message = vault.get_conversation("rick", "cleanup")
        self.assertEqual(message["title"], "Cleanup")
        self.assertIn("1 to archive", message["turns"][0]["content"])
        self.assertIn("/mobile/#/agents/rick/console", message["turns"][0]["content"])
        self.assertEqual(push.call_args.kwargs["url"], "/mobile/#/agents/rick/console")
        self.assertFalse(cleanup.is_due(datetime(2026, 10, 19, 12, 0), "2026-W43"), "once a week")

    def test_due_mondays_by_day_not_at_night(self):
        self.assertTrue(cleanup.is_due(datetime(2026, 10, 19, 9, 0), "2026-W42"))   # Monday 09:00
        self.assertFalse(cleanup.is_due(datetime(2026, 10, 19, 8, 59), "2026-W42"))
        self.assertTrue(cleanup.is_due(datetime(2026, 10, 21, 14, 0), "2026-W42"))  # missed: Wednesday
        self.assertFalse(cleanup.is_due(datetime(2026, 10, 21, 23, 0), "2026-W42"), "no push at night")


if __name__ == "__main__":
    unittest.main()
