import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import agents
import ha_guard
import llm_client
import reminders
import scheduled_tasks
import vault
import web_push

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "Agents"
NOW = datetime(2026, 10, 23, 12, 0)  # a Friday
HA_UPDATE = {"title": "Weekly HA update", "instructions": "Update Home Assistant and check for breaking changes.",
             "days": ["sun"], "time": "10:00", "preapproved": "update.install, homeassistant.restart"}


class ScheduledTasksTests(unittest.TestCase):
    """Against synthetic agent folders; clock pinned; push and agent runs mocked."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        root = tmp / "Agents"
        root.mkdir()
        for name in ("Spice", "R2"):
            shutil.copytree(FIXTURE_ROOT / name, root / name)
        for patcher in (
            patch.object(agents, "AGENTS_ROOT", root),
            patch.object(vault, "AGENTS_ROOT", root),
            patch.object(reminders, "now_local", lambda: NOW),
            patch.object(web_push, "send_all", return_value=1),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.dir = root / "R2"

    def test_add_validates_and_schedules_next_matching_day(self):
        for bad in ({**HA_UPDATE, "title": ""}, {**HA_UPDATE, "instructions": " "}, {**HA_UPDATE, "days": []},
                    {**HA_UPDATE, "days": ["sunday", "funday"]}, {**HA_UPDATE, "time": "25:00"},
                    {**HA_UPDATE, "preapproved": "rm -rf"}):
            with self.assertRaises(ValueError, msg=bad):
                scheduled_tasks.add("r2", bad)
        task = scheduled_tasks.add("r2", HA_UPDATE)
        self.assertEqual(task["next_at"], "2026-10-25T10:00")
        self.assertEqual(task["schedule"], "Every Sunday at 10:00")
        self.assertEqual(task["preapproved"], ["update.install", "homeassistant.restart"])
        self.assertEqual(task["conversation_id"], f"task-{task['id']}")
        full = scheduled_tasks.add("r2", {**HA_UPDATE, "preapproved": "*"})
        self.assertEqual(full["preapproved"], ["*"])
        self.assertIn("any Home Assistant change", scheduled_tasks.run_prompt(full))
        self.assertTrue(vault.is_protected_workspace_file("tasks.json"))
        stored = json.loads((self.dir / "tasks.json").read_text())
        self.assertEqual(stored["tasks"][0]["title"], "Weekly HA update")

    def test_next_run_same_day_later_and_multiple_days(self):
        self.assertEqual(scheduled_tasks.next_run(["fri"], "13:00", NOW), datetime(2026, 10, 23, 13, 0))
        self.assertEqual(scheduled_tasks.next_run(["fri"], "12:00", NOW), datetime(2026, 10, 30, 12, 0))
        self.assertEqual(scheduled_tasks.next_run(["mon", "wed"], "08:00", NOW), datetime(2026, 10, 26, 8, 0))

    def test_edit_moves_schedule_and_pause_stops_runs(self):
        task = scheduled_tasks.add("r2", HA_UPDATE)
        edited = scheduled_tasks.update("r2", task["id"], {"days": ["sat"], "time": "09:30"})
        self.assertEqual(edited["next_at"], "2026-10-24T09:30")
        self.assertEqual(edited["instructions"], HA_UPDATE["instructions"], "unchanged fields kept")
        paused = scheduled_tasks.update("r2", task["id"], {"enabled": False})
        self.assertIsNone(paused["next_at"])
        self.assertEqual(scheduled_tasks.take_due("r2", datetime(2026, 10, 24, 9, 30)), [])
        self.assertIsNone(scheduled_tasks.update("r2", "nope", {"enabled": True}))
        self.assertTrue(scheduled_tasks.delete("r2", task["id"]))
        self.assertEqual(scheduled_tasks.list_for("r2"), [])

    def test_due_task_runs_once_and_moves_a_week_on(self):
        task = scheduled_tasks.add("r2", HA_UPDATE)
        self.assertEqual(scheduled_tasks.take_due("r2", datetime(2026, 10, 25, 9, 59)), [])
        due = scheduled_tasks.take_due("r2", datetime(2026, 10, 25, 10, 0))
        self.assertEqual([d["id"] for d in due], [task["id"]])
        self.assertEqual(due[0]["scheduled_for"], "2026-10-25T10:00")
        self.assertEqual(scheduled_tasks.get("r2", task["id"])["next_at"], "2026-11-01T10:00")
        self.assertEqual(scheduled_tasks.take_due("r2", datetime(2026, 10, 25, 10, 1)), [])

    def test_late_run_within_limit_runs_older_miss_is_logged(self):
        task = scheduled_tasks.add("r2", HA_UPDATE)
        self.assertEqual(len(scheduled_tasks.take_due("r2", datetime(2026, 10, 25, 15, 0))), 1, "5h late: runs")
        self.assertEqual(scheduled_tasks.take_due("r2", datetime(2026, 11, 2, 9, 0)), [], "23h late: skipped")
        got = scheduled_tasks.get("r2", task["id"])
        self.assertEqual(got["last_run"]["status"], "missed")
        self.assertEqual(got["next_at"], "2026-11-08T10:00")

    def test_run_reports_in_its_conversation_with_preapproved_env(self):
        import chat_service
        task = scheduled_tasks.add("r2", HA_UPDATE)
        seen = {}

        def fake_run(agent_id, conversation_id, message, attachments=None, **kw):
            seen.update(conversation=conversation_id, message=message, env=dict(llm_client.RUN_ENV.get(agent_id, {})),
                        servers=llm_client._extra_servers(agent_id))
            return "STATUS: needs you\nUpdated to 2026.11.0. The old `platform: template` sensors broke; may I migrate them?"
        due = scheduled_tasks.take_due("r2", datetime(2026, 10, 25, 10, 0))
        with patch.object(chat_service, "run_chat_locked", side_effect=fake_run):
            entry = scheduled_tasks.run("r2", due[0])
        self.assertEqual(seen["conversation"], task["conversation_id"])
        self.assertTrue(seen["message"].startswith("(Scheduled task: Weekly HA update) Update Home Assistant"))
        self.assertIn("update.install, homeassistant.restart", seen["message"])
        self.assertEqual(seen["env"], {ha_guard.PREAPPROVED_ENV: "update.install,homeassistant.restart"})
        self.assertEqual(seen["servers"]["ha-admin"]["env"][ha_guard.PREAPPROVED_ENV], "update.install,homeassistant.restart")
        self.assertNotIn("r2", llm_client.RUN_ENV, "only for that run")
        self.assertEqual(entry["status"], "needs_you")
        self.assertTrue(entry["summary"].startswith("Updated to 2026.11.0"))
        got = scheduled_tasks.get("r2", task["id"])
        self.assertEqual(got["last_run"]["status"], "needs_you")
        convo = vault.get_conversation("r2", task["conversation_id"])
        self.assertEqual(convo["title"], "Task: Weekly HA update")
        kwargs = web_push.send_all.call_args.kwargs
        self.assertEqual(kwargs["title"], "R2 · Weekly HA update")
        self.assertTrue(kwargs["body"].startswith("Needs you: Updated"))
        self.assertIn(f"/r2/conversations/task-{task['id']}", kwargs["url"])

    def test_failed_run_is_recorded_and_pushed(self):
        import chat_service
        task = scheduled_tasks.add("spice", {**HA_UPDATE, "preapproved": ""})
        with patch.object(chat_service, "run_chat_locked", side_effect=RuntimeError("model unavailable")):
            entry = scheduled_tasks.run("spice", scheduled_tasks._load("spice")["tasks"][0], manual=True)
        self.assertEqual(entry["status"], "failed")
        self.assertTrue(entry["manual"])
        self.assertIn("model unavailable", vault.get_conversation("spice", task["conversation_id"])["turns"][-1]["content"])
        self.assertTrue(web_push.send_all.call_args.kwargs["body"].startswith("Failed:"))

    def test_status_line_parsing(self):
        self.assertEqual(scheduled_tasks.split_status("STATUS: ok\nAll fine."), ("ok", "All fine."))
        self.assertEqual(scheduled_tasks.split_status("**STATUS: failed**\nNo HA."), ("failed", "No HA."))
        self.assertEqual(scheduled_tasks.split_status("Status: Needs you\nx"), ("needs_you", "x"))
        self.assertEqual(scheduled_tasks.split_status("Just a report."), ("ok", "Just a report."))

    def test_task_conversations_skipped_by_cleanup_and_listed_in_prompt(self):
        import cleanup
        task = scheduled_tasks.add("r2", HA_UPDATE)
        vault.append_turn("r2", task["conversation_id"], "assistant", "Each run reports here.", first_message="Task: x")
        self.assertNotIn(task["conversation_id"],
                         [i["id"] for i in vault.list_conversations("r2")
                          if i["id"] not in cleanup.SKIP_IDS and not scheduled_tasks.is_task_conversation(i["id"])])
        self.assertIn("'Weekly HA update' (Every Sunday at 10:00)", scheduled_tasks.prompt_line("r2"))
        self.assertEqual(scheduled_tasks.prompt_line("spice"), "")

    def test_codex_task_run_sets_preapproved_env(self):
        configs = []

        def fake_run(args, **kwargs):
            configs.append(tomllib.loads((Path(kwargs["env"]["CODEX_HOME"]) / "config.toml").read_text()))
            Path(args[args.index("--output-last-message") + 1]).write_text("ok")
            return subprocess.CompletedProcess(args, 0, "", "")
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as root, \
             patch.dict(os.environ, {"CODEX_HOME": home, "HA_API_TOKEN": "t"}), \
             patch.object(llm_client.vault, "agent_root", side_effect=lambda a: Path(root) / a), \
             patch.object(llm_client.subprocess, "run", side_effect=fake_run):
            llm_client.RUN_ENV["r2"] = {ha_guard.PREAPPROVED_ENV: "update.install"}
            try:
                llm_client._chat_codex_cli("r2", "gpt-6-sol", "s", [])
            finally:
                llm_client.RUN_ENV.pop("r2", None)
        self.assertEqual(configs[0]["mcp_servers"]["openclave-ha-admin"]["env"], {ha_guard.PREAPPROVED_ENV: "update.install"})

    def test_codex_gets_literal_server_env_from_agent_data(self):
        configs = []

        def fake_run(args, **kwargs):
            configs.append(tomllib.loads((Path(kwargs["env"]["CODEX_HOME"]) / "config.toml").read_text()))
            Path(args[args.index("--output-last-message") + 1]).write_text("ok")
            return subprocess.CompletedProcess(args, 0, "", "")
        cfg = agents.AGENTS["r2"]
        servers = {**cfg.mcp_servers, "ha-admin": {**cfg.mcp_servers["ha-admin"],
                                                   "env": {"HA_API_TOKEN": "${HA_API_TOKEN}", "HA_BASE_URL": "http://ha.invalid:8123"}}}
        import dataclasses
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as root, \
             patch.dict(os.environ, {"CODEX_HOME": home, "HA_API_TOKEN": "t"}), \
             patch.dict(agents.AGENTS, {"r2": dataclasses.replace(cfg, mcp_servers=servers)}), \
             patch.object(llm_client.vault, "agent_root", side_effect=lambda a: Path(root) / a), \
             patch.object(llm_client.subprocess, "run", side_effect=fake_run):
            llm_client._chat_codex_cli("r2", "gpt-6-sol", "s", [])
        admin = configs[0]["mcp_servers"]["openclave-ha-admin"]
        self.assertEqual(admin["env"], {"HA_BASE_URL": "http://ha.invalid:8123"}, "the token is forwarded, never written")
        self.assertEqual(admin["env_vars"], ["HA_API_TOKEN"])


class PreapprovedGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(ha_guard, "STATE_FILE", Path(tmp.name) / "ha_approvals.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_action_names(self):
        self.assertEqual(ha_guard.action_name({"api": "rest", "method": "POST", "path": "/api/services/update/install"}),
                         "update.install")
        self.assertEqual(ha_guard.action_name({"api": "ws", "message": {"type": "call_service", "domain": "update",
                                                                        "service": "install"}}), "update.install")
        self.assertEqual(ha_guard.action_name({"api": "ws", "message": {"type": "backup/generate"}}), "backup/generate")

    def test_only_listed_actions_skip_the_yes(self):
        allowed = "update.install,backup/generate"
        self.assertTrue(ha_guard.preapproved({"api": "rest", "method": "POST", "path": "/api/services/update/install"}, allowed))
        self.assertTrue(ha_guard.preapproved({"api": "ws", "message": {"type": "backup/generate"}}, allowed))
        self.assertFalse(ha_guard.preapproved({"api": "rest", "method": "POST", "path": "/api/services/script/turn_on"}, allowed))
        self.assertFalse(ha_guard.preapproved({"api": "rest", "method": "POST", "path": "/api/services/update/install"}, ""))
        self.assertFalse(ha_guard.preapproved({"api": "rest", "method": "POST", "path": "/api/services/update/install"}, None))
        self.assertTrue(ha_guard.preapproved({"api": "ws", "message": {"type": "config/entity_registry/update"}}, "*"))

    def test_mcp_server_runs_preapproved_and_still_refuses_the_rest(self):
        with patch.dict(os.environ, {"HA_API_TOKEN": "test", ha_guard.PREAPPROVED_ENV: "update.install"}):
            import mcp_ha_server
            response = unittest.mock.MagicMock()
            response.__enter__.return_value.read.return_value = b"[]"
            with patch.object(mcp_ha_server.urllib.request, "urlopen", return_value=response) as opened:
                mcp_ha_server.ha_api("POST", "/api/services/update/install", {"entity_id": "update.home_assistant_core_update"})
                self.assertEqual(opened.call_count, 1)
                refused = mcp_ha_server.ha_api("POST", "/api/services/homeassistant/restart", {})
                self.assertEqual(opened.call_count, 1)
        self.assertIn("NOT DONE", refused)


if __name__ == "__main__":
    unittest.main()
