import json
import shutil
import tempfile
import unittest
import unittest.mock
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import agents
import llm_client
import reminders
import vault
import web_push

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "Agents"
NOW = datetime(2026, 9, 25, 19, 0)


class RemindersTests(unittest.TestCase):
    """Against a synthetic agent folder; clock pinned; push mocked."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        root = tmp / "Agents"
        root.mkdir()
        shutil.copytree(FIXTURE_ROOT / "Spice", root / "Spice")
        for patcher in (
            patch.object(agents, "AGENTS_ROOT", root),
            patch.object(vault, "AGENTS_ROOT", root),
            patch.object(reminders, "now_local", lambda: NOW),
            patch.object(web_push, "send_all", return_value=1),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.dir = root / "Spice"

    def test_add_validates(self):
        for message, at, repeat in (("", "2026-10-01 10:00", None), ("x", "2026-09-25 18:00", None),
                                    ("x", "tomorrow", None), ("x", "2026-10-01 10:00", "hourly"),
                                    ("x" * 1001, "2026-10-01 10:00", None), ("x", "2030-01-01 10:00", None)):
            with self.assertRaises(ValueError, msg=(message[:5], at, repeat)):
                reminders.add("spice", message, at, repeat)
        r = reminders.add("spice", "Water the basil", "2026-09-27 18:00", "weekly")
        self.assertEqual(r["next_at"], "2026-09-27T18:00")
        self.assertEqual(reminders.list_for("spice")[0]["id"], r["id"])
        self.assertTrue(vault.is_protected_workspace_file("reminders.json"))

    def test_one_off_fires_once_and_is_logged(self):
        r = reminders.add("spice", "How did the race go?", "2026-09-25 19:30")
        self.assertEqual(reminders.take_due("spice", datetime(2026, 9, 25, 19, 29)), [])
        due = reminders.take_due("spice", datetime(2026, 9, 25, 19, 30))
        self.assertEqual([d["id"] for d in due], [r["id"]])
        self.assertEqual(reminders.list_for("spice"), [])
        self.assertEqual(reminders.history_for("spice")[0]["message"], "How did the race go?")
        self.assertEqual(reminders.take_due("spice", datetime(2026, 9, 25, 19, 31)), [])

    def test_repeats_move_forward_and_skip_missed(self):
        reminders.add("spice", "weekly", "2026-09-27 18:00", "weekly")
        reminders.take_due("spice", datetime(2026, 10, 12, 9, 0))  # service down for two weeks
        self.assertEqual(reminders.list_for("spice")[0]["next_at"], "2026-10-18T18:00")
        reminders.add("spice", "monthly", "2027-01-31 08:00", "monthly")
        reminders.take_due("spice", datetime(2027, 1, 31, 8, 0))
        monthly = [r for r in reminders.list_for("spice") if r["repeat"] == "monthly"][0]
        self.assertEqual(monthly["next_at"], "2027-02-28T08:00")

    def test_deliver_posts_into_reminders_conversation_and_pushes(self):
        reminders.add("spice", "Try the new ramen place", "2026-09-25 19:05")
        with patch.object(reminders, "now_local", lambda: datetime(2026, 9, 25, 19, 5)):
            self.assertEqual(reminders.run_due_once(), 1)
        convo = vault.get_conversation("spice", reminders.CONVERSATION_ID)
        self.assertEqual(convo["title"], "Reminders")
        self.assertEqual(convo["turns"][-1]["role"], "assistant")
        self.assertIn("ramen", convo["turns"][-1]["content"])
        kwargs = web_push.send_all.call_args.kwargs
        self.assertEqual(kwargs["title"], "Spice")
        self.assertIn("/spice/conversations/reminders", kwargs["url"])
        self.assertTrue(vault.latest_activity("spice")["last_is_reply"])

    def test_compose_reminder_is_written_at_send_time_look_up_only(self):
        import chat_service
        r = reminders.add("spice", "Check tomorrow's rain forecast; tell the owner whether to water the basil",
                          "2026-09-26 08:00", compose=True)
        self.assertTrue(r["compose"])
        seen = {}

        def fake_run(agent_id, conversation_id, message, attachments=None, **kw):
            seen.update(agent=agent_id, conversation=conversation_id, message=message,
                        read_only=agent_id in llm_client.READ_ONLY_RUNS)
            return "Rain from 14:00 tomorrow: skip watering."
        due = reminders.take_due("spice", datetime(2026, 9, 26, 8, 0))
        with patch.object(chat_service, "run_chat_locked", side_effect=fake_run):
            reminders.deliver("spice", due[0])
        self.assertEqual(seen["conversation"], reminders.CONVERSATION_ID)
        self.assertTrue(seen["message"].startswith("(Scheduled reminder) Check tomorrow's rain forecast"))
        self.assertTrue(seen["read_only"], "the run is look-up only")
        self.assertNotIn("spice", llm_client.READ_ONLY_RUNS, "and only for that run")
        web_push.send_all.assert_called_with(title="Spice", body="Rain from 14:00 tomorrow: skip watering.",
                                             url="/mobile/#/agents/spice/conversations/reminders", tag=unittest.mock.ANY)
        self.assertTrue(reminders.history_for("spice")[0]["compose"])

    def test_compose_failure_still_reminds(self):
        import chat_service
        reminders.add("spice", "Check the basil soil sensor", "2026-09-26 08:00", compose=True)
        due = reminders.take_due("spice", datetime(2026, 9, 26, 8, 0))
        with patch.object(chat_service, "run_chat_locked", side_effect=RuntimeError("model unavailable")):
            reminders.deliver("spice", due[0])
        body = web_push.send_all.call_args.kwargs["body"]
        self.assertIn("Reminder: Check the basil soil sensor", body)
        self.assertIn("couldn't prepare", body)
        turns = vault.get_conversation("spice", reminders.CONVERSATION_ID)["turns"]
        self.assertIn("couldn't prepare", turns[-1]["content"])

    def test_read_only_run_limits_r2_home_assistant(self):
        import ha_guard
        normal = llm_client._extra_servers("r2")
        self.assertIn("ha", normal)
        llm_client.READ_ONLY_RUNS.add("r2")
        try:
            limited = llm_client._extra_servers("r2")
        finally:
            llm_client.READ_ONLY_RUNS.discard("r2")
        self.assertNotIn("ha", limited, "HA's own device-control server isn't loaded")
        self.assertEqual(limited["ha-admin"]["env"][ha_guard.READ_ONLY_ENV], "1")
        self.assertNotIn(ha_guard.READ_ONLY_ENV, llm_client._extra_servers("r2")["ha-admin"].get("env", {}), "agent config untouched")
        with patch.dict("os.environ", {"HA_API_TOKEN": "test", ha_guard.READ_ONLY_ENV: "1"}):
            import mcp_ha_server
            with patch.object(mcp_ha_server.urllib.request, "urlopen") as opened:
                refused = mcp_ha_server.ha_api("POST", "/api/services/light/turn_on", {"entity_id": "light.x"})
                self.assertEqual(mcp_ha_server.ha_ws({"type": "call_service", "domain": "light", "service": "turn_on"}), ha_guard.READ_ONLY_REFUSAL)
            opened.assert_not_called()
        self.assertEqual(refused, ha_guard.READ_ONLY_REFUSAL)

    def test_codex_read_only_run_sets_ha_env(self):
        import os, subprocess, tomllib
        configs = []
        def fake_run(args, **kwargs):
            configs.append(tomllib.loads((Path(kwargs["env"]["CODEX_HOME"]) / "config.toml").read_text()))
            Path(args[args.index("--output-last-message") + 1]).write_text("ok")
            return subprocess.CompletedProcess(args, 0, "", "")
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as root, \
             patch.dict(os.environ, {"CODEX_HOME": home, "HA_API_TOKEN": "t"}), \
             patch.object(llm_client.vault, "agent_root", side_effect=lambda a: Path(root) / a), \
             patch.object(llm_client.subprocess, "run", side_effect=fake_run):
            llm_client._chat_codex_cli("r2", "gpt-6-sol", "s", [])
            llm_client.READ_ONLY_RUNS.add("r2")
            try:
                llm_client._chat_codex_cli("r2", "gpt-6-sol", "s", [])
            finally:
                llm_client.READ_ONLY_RUNS.discard("r2")
        self.assertNotIn("env", configs[0]["mcp_servers"]["openclave-ha-admin"])
        self.assertEqual(configs[1]["mcp_servers"]["openclave-ha-admin"]["env"], {"OPENCLAVE_HA_READ_ONLY": "1"})

    def test_unreadable_file_is_treated_as_empty(self):
        (self.dir / "reminders.json").write_text("{nope")
        self.assertEqual(reminders.list_for("spice"), [])
        self.assertEqual(reminders.take_due("spice"), [])

    def test_tool_dispatch(self):
        with patch("agent_tools.allowed", return_value={"schedule_reminder"}):
            out = llm_client._run_tool("spice", "schedule_reminder",
                                       {"action": "add", "message": "hi", "at": "2026-09-26 09:00"})
            self.assertIn("Saturday 2026-09-26 09:00", out)
            rid = out.split("id ")[1].split(")")[0]
            self.assertIn(rid, llm_client._run_tool("spice", "schedule_reminder", {"action": "list"}))
            self.assertIn("not scheduled", llm_client._run_tool("spice", "schedule_reminder",
                                                                {"action": "add", "message": "hi", "at": "2020-01-01 09:00"}))
            self.assertEqual(llm_client._run_tool("spice", "schedule_reminder", {"action": "cancel", "reminder_id": rid}), "cancelled")


if __name__ == "__main__":
    unittest.main()
