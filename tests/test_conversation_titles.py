"""Auto-naming new conversations: after the first
exchange, a short name replaces the starting title, never a name the owner gave."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import conversation_titles as ct
import llm_client
import vault
from agents import AGENTS, AgentConfig

USAGE = {"input_tokens": 1, "output_tokens": 1}


class _GregVault(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        registry = patch.dict(AGENTS, {"greg": AgentConfig("greg", "Greg", "Greg", "#6a8f60", "Synthetic test agent")})
        registry.start()
        self.addCleanup(registry.stop)
        for name, value in (("VAULT_ROOT", Path(self.temp.name).resolve()),):
            patcher = patch.object(vault, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(vault, "AGENTS_ROOT", (vault.VAULT_ROOT / "OpenClave" / "Agents").resolve())
        patcher.start()
        self.addCleanup(patcher.stop)
        vault.agent_root("greg").mkdir(parents=True)
        patcher = patch.object(llm_client, "log_usage")
        self.log_usage = patcher.start()
        self.addCleanup(patcher.stop)

    def exchange(self, cid, message="lets integrate the panasonic heat pump\nmore details", reply="Sure."):
        vault.append_turn("greg", cid, "user", message, first_message=message)
        vault.append_turn("greg", cid, "assistant", reply)

    def name(self, cid, generated="Heat pump integration"):
        with patch.object(ct, "_generate", return_value=(generated, USAGE)) as gen:
            return ct.name_now("greg", cid), gen

    def title(self, cid):
        return vault.get_conversation("greg", cid)["title"]


class ConversationTitleTests(_GregVault):

    def test_one_click_auto_rename_replaces_any_name_and_keeps_mtime(self):
        self.exchange("c1")
        vault.rename_conversation("greg", "c1", "Something old")
        f = vault.conversation_file("greg", "c1")
        before = f.stat().st_mtime_ns
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), \
                patch.object(ct, "_generate", return_value=("Heat pump integration", USAGE)):
            self.assertEqual(ct.rename_now("greg", "c1"), "Heat pump integration")
        self.assertEqual(self.title("c1"), "Heat pump integration")
        self.assertEqual(f.stat().st_mtime_ns, before)  # not shown as new
        self.assertIsNone(ct.rename_now("greg", "missing"))

    def test_first_exchange_gets_a_short_name(self):
        self.exchange("c1")
        self.assertEqual(self.title("c1"), "lets integrate the panasonic heat pump")
        new, gen = self.name("c1", '"Heat pump integration."')
        self.assertEqual(new, "Heat pump integration")
        self.assertEqual(self.title("c1"), "Heat pump integration")
        self.assertEqual(gen.call_args.args[0].splitlines()[0], "lets integrate the panasonic heat pump")
        self.log_usage.assert_called_once()

    def test_a_name_the_owner_gave_is_never_replaced(self):
        self.exchange("c1")
        vault.rename_conversation("greg", "c1", "My heat pump")
        new, gen = self.name("c1")
        self.assertIsNone(new)
        gen.assert_not_called()
        self.assertEqual(self.title("c1"), "My heat pump")

    def test_renamed_while_the_name_was_being_made(self):
        self.exchange("c1")
        def owner_renames(*a):
            vault.rename_conversation("greg", "c1", "Mine")
            return "Heat pump integration", USAGE
        with patch.object(ct, "_generate", side_effect=owner_renames):
            self.assertIsNone(ct.name_now("greg", "c1"))
        self.assertEqual(self.title("c1"), "Mine")

    def test_only_after_the_first_exchange(self):
        self.exchange("c1")
        vault.append_turn("greg", "c1", "user", "and another thing")
        vault.append_turn("greg", "c1", "assistant", "ok")
        self.assertIsNone(self.name("c1")[0])
        # Conversations that start with the agent (reminders, handovers) keep their names.
        vault.append_turn("greg", "c2", "assistant", "Reminders you scheduled arrive here.", first_message="Reminders")
        vault.append_turn("greg", "c2", "user", "thanks")
        self.assertIsNone(self.name("c2")[0])

    def test_empty_or_unchanged_names_are_ignored(self):
        self.exchange("c1")
        self.assertIsNone(self.name("c1", "")[0])
        self.assertIsNone(self.name("c1", "lets integrate the panasonic heat pump")[0])

    def test_his_own_topic_label_is_used_without_a_model(self):
        self.exchange("c1", message="Planting av tre. Denne samtalen blir om epletrærne")
        new, gen = self.name("c1")
        self.assertEqual(new, "Planting av tre")
        gen.assert_not_called()
        self.assertEqual(ct.label_title("Aliexpress list: in this conversation"), "Aliexpress list")
        self.assertEqual(ct.label_title("heatpumps. lets go"), "Heatpumps")
        for no_label in ("From Stash: please add", "lower back is killing me today. help",
                         "In HA, what should we do next?", "v1.2 is out", "So. what now"):
            self.assertIsNone(ct.label_title(no_label), no_label)

    def test_clean(self):
        self.assertEqual(ct.clean("Title: **Bike parts**."), "Bike parts")
        self.assertEqual(ct.clean("Tesla financial modeling\nextra line"), "Tesla financial modeling")
        long = ct.clean("Gear for outdoor leather belt and pouches and other assorted things")
        self.assertLessEqual(len(long), ct.MAX_LEN)
        self.assertFalse(long.endswith(" "))

    def test_schedule_is_off_without_a_key(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(ct, "name_now") as name_now:
            ct.schedule("greg", "c1")
        name_now.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class RenameKeepsActivityTests(_GregVault):
    """Renaming must not light the mobile "new" tag:
    only an appended agent reply moves a conversation's activity time."""

    def activity(self, cid):
        return next(c["updated_ms"] for c in vault.list_conversations("greg") if c["id"] == cid)

    def age(self, cid, seconds=3600):
        f = vault.conversation_file("greg", cid)
        old = f.stat().st_mtime_ns - seconds * 1_000_000_000
        os.utime(f, ns=(old, old))
        return old // 1_000_000

    def test_rename_keeps_activity_time(self):
        self.exchange("c1")
        before = self.age("c1")
        vault.rename_conversation("greg", "c1", "Renamed")
        self.assertEqual(self.title("c1"), "Renamed")
        self.assertEqual(self.activity("c1"), before)
        self.assertEqual(vault.latest_activity("greg")["last_activity_ms"], before)

    def test_auto_name_keeps_activity_time(self):
        self.exchange("c1")
        before = self.age("c1")
        self.name("c1")
        self.assertEqual(self.title("c1"), "Heat pump integration")
        self.assertEqual(self.activity("c1"), before)

    def test_a_new_reply_still_moves_it(self):
        self.exchange("c1")
        before = self.age("c1")
        vault.append_turn("greg", "c1", "assistant", "More.")
        self.assertGreater(self.activity("c1"), before)
