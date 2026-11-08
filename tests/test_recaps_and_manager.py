"""Recaps for archived chats, the manager role, its proposals and weekly
audit (2026-11-03). Synthetic vault in a temp dir; no model calls."""
import dataclasses
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_tools
import agents
import audit
import chat_jobs
import proposals
import recaps
import vault
from agents import AGENTS, AgentConfig

DAY = 86400


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.root = root
        greg = AgentConfig("greg", "Greg", "Greg", "#6a8f60", "Synthetic test agent")
        boss = agents._agent_config({"id": "boss", "name": "Boss", "color": "#000", "tagline": "t",
                                     "roles": ["maintainer"]}, "Boss")
        registry = patch.dict(AGENTS, {"greg": greg, "boss": boss}, clear=True)
        registry.start()
        self.addCleanup(registry.stop)
        config = dataclasses.replace(vault.CONFIG, confidential_folders=("Secret",), owner_name="")
        for target, name, value in (
            (vault, "VAULT_ROOT", root),
            (vault, "AGENTS_ROOT", root / "OpenClave" / "Agents"),
            (vault, "CONFIG", config),
            (proposals, "CONFIG", config),
            (audit, "CONFIG", config),
            (proposals, "STATE_FILE", root / "proposals.json"),
            (audit, "STATE_FILE", root / "audit.json"),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        for agent in ("greg", "boss"):
            vault.agent_root(agent).mkdir(parents=True)
        (root / "Home" / "Garden").mkdir(parents=True)
        (root / "Secret").mkdir()

    def chat(self, cid, agent="greg"):
        vault.append_turn(agent, cid, "user", "Tomatoes: north or south bed?", first_message="Tomatoes")
        vault.append_turn(agent, cid, "assistant", "South bed: more sun. Plant after May 17.")


class RecapTests(Base):
    def test_parse(self):
        good = recaps.parse('Sure: {"about": "Picked the south bed.", "status": "Concluded", "open_ends": "none"}')
        self.assertEqual(good, {"about": "Picked the south bed.", "status": "concluded", "open_ends": ""})
        self.assertIsNone(recaps.parse('{"about": "x", "status": "maybe"}'))
        self.assertIsNone(recaps.parse("no json"))

    def test_archive_then_recap_keeps_transcript_and_last_active(self):
        self.chat("c1")
        old = time.time() - 10 * DAY
        os.utime(vault.conversation_file("greg", "c1"), (old, old))
        with patch.object(recaps, "schedule") as scheduled:
            self.assertTrue(chat_jobs.archive_conversation("greg", "c1"))
        scheduled.assert_called_once_with("greg", "c1")
        self.assertEqual(vault.list_conversations("greg"), [])

        seen = {}

        def fake(title, transcript):
            seen.update(title=title, transcript=transcript)
            return '{"about": "South bed for tomatoes, after May 17.", "status": "concluded", "open_ends": ""}', None

        recap = recaps.make_now("greg", "c1", generate=fake)
        self.assertEqual(recap["status"], "concluded")
        self.assertEqual(seen["title"], "Tomatoes")
        self.assertIn("Person: Tomatoes: north or south bed?", seen["transcript"])
        f = vault.archived_conversation_file("greg", "c1")
        self.assertAlmostEqual(f.stat().st_mtime, old, delta=1, msg="last active stays the chat's own")
        convo = vault.get_conversation("greg", "c1", archived=True)
        self.assertEqual(len(convo["turns"]), 2, "the transcript is still there")
        [card] = recaps.list_for("greg")
        self.assertEqual((card["id"], card["status"], card["about"]), ("c1", "concluded", "South bed for tomatoes, after May 17."))
        self.assertTrue(vault.unarchive_conversation("greg", "c1"))
        self.assertEqual(recaps.list_for("greg"), [])

    def test_a_new_message_brings_it_back(self):
        self.chat("c3")
        vault.archive_conversation("greg", "c3")
        self.assertIsNone(vault.get_conversation("greg", "c3"))
        with patch.object(chat_jobs, "_EXECUTOR"):
            chat_jobs.submit("greg", "req-1", "c3", "One more question")
        self.assertIsNotNone(vault.get_conversation("greg", "c3"), "back in the chat list")
        self.assertEqual(recaps.list_for("greg"), [])
        self.assertFalse(vault.restore_if_archived("greg", "c3"), "already live")
        self.assertFalse(vault.restore_if_archived("greg", "nope"))
        self.assertFalse(vault.restore_if_archived("greg", None))

    def test_no_recap_yet_and_bad_reply(self):
        self.chat("c2")
        vault.archive_conversation("greg", "c2")
        self.assertIsNone(recaps.list_for("greg")[0]["status"])
        self.assertIsNone(recaps.make_now("greg", "c2", generate=lambda t, x: ("nope", None)))
        self.assertIsNone(recaps.list_for("greg")[0]["status"])

    def test_schedule_is_off_without_a_key(self):
        with patch.dict(os.environ, {}, clear=True), patch("threading.Thread") as thread:
            recaps.schedule("greg", "x")
        thread.assert_not_called()


class ManagerRoleTests(Base):
    def test_old_and_new_role_names(self):
        self.assertEqual(agents.manager_id(), "boss")
        self.assertTrue(agents.has_role("boss", "manager") and agents.has_role("boss", "maintainer"))
        new = agents._agent_config({"id": "m", "name": "M", "color": "#000", "tagline": "t", "roles": ["manager"]}, "M")
        self.assertIn("maintainer", new.roles)
        self.assertIsNone(agents.manager_problem())

    def test_manager_problem(self):
        with patch.dict(AGENTS, {"boss": AgentConfig("boss", "Boss", "Boss", "#000", "t")}):
            self.assertIn("No manager", agents.manager_problem())
        two = agents._agent_config({"id": "b2", "name": "B2", "color": "#000", "tagline": "t", "roles": ["manager"]}, "B2")
        with patch.dict(AGENTS, {"b2": two}):
            self.assertIn("More than one manager", agents.manager_problem())

    def test_propose_tool_only_for_the_manager(self):
        self.assertIn("propose_to_owner", agent_tools.allowed("boss"))
        self.assertNotIn("propose_to_owner", agent_tools.allowed("greg"))
        self.assertTrue(proposals.tool_reply("greg", {}).startswith("refused"))


class MainVaultWriteTests(Base):
    def test_append_before_changelog_with_stamp(self):
        note = self.root / "Home" / "Garden" / "Tomatoes.md"
        note.write_text("---\nName: Tomatoes\n---\n\n## Summary\nGrow them.\n\n## Changelog\n- 2026-01-01 first\n")
        vault.write_main_vault_note("Home/Garden/Tomatoes.md", "## Beds\n- South bed, after May 17", "append", "Bed choice from Greg")
        text = note.read_text()
        self.assertLess(text.index("## Beds"), text.index("## Changelog"))
        self.assertRegex(text, r"- 2026-01-01 first\n- \d{4}-\d{2}-\d{2} \[BOT\] Bed choice from Greg\n$")

    def test_create_and_refusals(self):
        vault.write_main_vault_note("Home/Garden/Beds.md", "# Beds\nSouth.", "create", "New")
        self.assertTrue((self.root / "Home" / "Garden" / "Beds.md").exists())
        for path, mode in (("Home/Garden/Beds.md", "create"), ("Home/Garden/Missing.md", "append"),
                           ("Home/Nofolder/New.md", "create"), ("Secret/x.md", "create"),
                           ("OpenClave/Agents/Greg/x.md", "create"), ("../out.md", "create"),
                           ("Home/Garden/x.txt", "create"), (".obsidian/x.md", "create")):
            with self.assertRaises(vault.MainVaultError, msg=path):
                vault.write_main_vault_note(path, "text", mode, "x")
        with self.assertRaises(vault.MainVaultError):
            vault.write_main_vault_note("Home/Garden/Bad.md", "---\n: [\n---\nx", "create", "x")


class ProposalTests(Base):
    def propose(self, **kw):
        args = {"kind": "vault_note", "title": "Tomato beds", "why": "Settled.", "content": "## Beds\n- South",
                "target": "Home/Garden/Beds.md", "mode": "create", "source": "OpenClave/Agents/Greg/notes.md"}
        args.update(kw)
        return proposals.tool_reply("boss", args)

    def test_vault_note_yes_writes_no_forgets(self):
        self.assertIn("filed", self.propose())
        self.assertIn("already waiting", self.propose())
        self.assertIn("refused", self.propose(title="x", target="Secret/a.md"))
        self.assertIn("refused", self.propose(title="y", mode="append", target="Home/Garden/None.md"))
        [p] = proposals.list_open()
        result = proposals.approve(p["id"], "## Beds\n- South, edited")
        self.assertEqual(result["result"], "wrote Home/Garden/Beds.md")
        self.assertIn("edited", (self.root / "Home" / "Garden" / "Beds.md").read_text())
        self.assertEqual(proposals.list_open(), [])
        with self.assertRaises(LookupError):
            proposals.approve(p["id"])

        self.propose(title="Again", target="Home/Garden/Other.md")
        [p] = proposals.list_open()
        proposals.decline(p["id"], "not useful")
        self.assertEqual([(i["title"], i["status"], i.get("note")) for i in proposals.recent_answered()][-1],
                         ("Again", "declined", "not useful"))
        self.assertFalse((self.root / "Home" / "Garden" / "Other.md").exists())

    def test_message_and_task(self):
        self.assertIn("refused", self.propose(kind="message_agent", title="m", agent="nobody"))
        self.propose(kind="message_agent", title="Nudge Greg", agent="Greg", content="Your plan is stale.")
        self.propose(kind="task", title="Tidy skills", content="Merge two skills.")
        items = {p["kind"]: p for p in proposals.list_open()}
        with patch("agent_messages.send") as send:
            proposals.approve(items["message_agent"]["id"])
        send.assert_called_once_with("boss", "greg", "Your plan is stale.")
        with patch.object(chat_jobs, "submit") as submit, patch.object(chat_jobs, "kick"):
            proposals.approve(items["task"]["id"])
        args = submit.call_args[0]
        self.assertEqual((args[0], args[2]), ("boss", "audit"))
        self.assertIn("Approved: Tidy skills", args[3])
        self.assertEqual(vault.get_conversation("boss", "audit")["title"], "Audit")
        where = None
        self.propose(kind="task", title="Talk", content="x")
        where = proposals.discuss(proposals.list_open()[0]["id"])
        self.assertEqual((where["agent_id"], where["conversation_id"]), ("boss", "audit"))


class AuditTests(Base):
    def test_gathers_changed_files_not_chats(self):
        root = vault.agent_root("greg")
        (root / "notes.md").write_text("beds")
        (root / "mission.md").write_text("m")
        (root / "persona.md").write_text("p")
        old = root / "old.md"
        old.write_text("old")
        os.utime(old, (time.time() - 90 * DAY,) * 2)
        self.chat("c1")
        (vault.agent_root("boss") / "own.md").write_text("manager's own")
        since = time.time() - 30 * DAY
        names = sorted(Path(f["path"]).name for f in audit.changed_files("greg", since))
        self.assertEqual(names, ["mission.md", "notes.md"])
        material = audit.gather(since)
        self.assertEqual(list(material["files"]), ["greg"], "not the manager's own folder")
        text = audit.brief(material, "2026-10-01")
        self.assertIn("OpenClave/Agents/Greg/notes.md", text)
        self.assertIn("Secret", text, "confidential folders are named as off limits")

    def test_run_queues_the_manager_with_context(self):
        (vault.agent_root("greg") / "notes.md").write_text("beds")
        with patch.object(chat_jobs, "submit") as submit, patch.object(chat_jobs, "kick") as kick:
            result = audit.run("Weekly cleanup: 1 to archive.")
        self.assertTrue(result["queued"])
        args, kwargs = submit.call_args
        self.assertEqual((args[0], args[2]), ("boss", "audit"))
        self.assertIn("notes.md", kwargs["extra"]["context_note"])
        self.assertIn("propose_to_owner", kwargs["extra"]["context_note"])
        kick.assert_called_once_with("boss")
        self.assertTrue(json.loads((self.root / "audit.json").read_text())["last_ts"])
        with patch.dict(AGENTS, {"boss": AgentConfig("boss", "Boss", "Boss", "#000", "t")}):
            self.assertFalse(audit.run()["queued"])


if __name__ == "__main__":
    unittest.main()
