import shutil
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import agents
import attention
import reminders
import service_settings
import vault
import web_push

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "Agents"
TODAY = date(2026, 9, 27)


class AttentionTests(unittest.TestCase):
    """Against a temp copy of synthetic files written per test."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        root = tmp / "Agents"
        root.mkdir()
        shutil.copytree(FIXTURE_ROOT / "Spice", root / "Spice")
        for patcher in (
            patch.object(agents, "AGENTS_ROOT", root),
            patch.object(vault, "AGENTS_ROOT", root),
            patch.object(attention, "STATE_FILE", tmp / "digest_state.json"),
            patch.object(service_settings, "SETTINGS_FILE", tmp / "service_settings.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.dir = root / "Spice"
        (self.dir / "tracking.md").unlink(missing_ok=True)

    def write(self, name, updated, body):
        (self.dir / name).write_text(f"---\nUpdated: '{updated}'\n---\n\n{body}\n", encoding="utf-8")

    def test_fresh_agent_has_nothing(self):
        self.write("plan.md", "2026-09-25", "# Plan")
        self.write("backlog.md", "2026-09-25", "- [ ] one\n- [x] two")
        self.assertEqual(attention.check("spice", TODAY), [])

    def chatted(self, day):
        import os, time
        convo = self.dir / "conversations" / "20260901-100000-aaaaaaaa.md"
        convo.parent.mkdir(exist_ok=True)
        convo.write_text("---\nTitle: x\n---\n", encoding="utf-8")
        stamp = time.mktime(datetime(day.year, day.month, day.day, 12).timetuple())
        os.utime(convo, (stamp, stamp))

    def test_stale_plan_and_untouched_backlog(self):
        self.chatted(date(2026, 9, 26))
        self.write("plan.md", "2026-09-04", "# Plan")
        self.write("backlog.md", "2026-09-10", "- [ ] one\n- [ ] two\n- [x] done")
        kinds = {i["kind"]: i for i in attention.check("spice", TODAY)}
        self.assertTrue(kinds["plan"]["upkeep"] and kinds["backlog"]["upkeep"])
        self.assertEqual(attention.needs_you({"spice": list(kinds.values())}), {"spice": []}, "upkeep isn't the amber dot")
        self.assertEqual(kinds["plan"]["days"], 23)
        self.assertEqual((kinds["backlog"]["count"], kinds["backlog"]["days"]), (2, 17))
        self.write("backlog.md", "2026-09-10", "- [x] all done")
        self.assertNotIn("backlog", {i["kind"] for i in attention.check("spice", TODAY)})

    def test_idle_agent_gets_no_upkeep_findings(self):
        self.chatted(date(2026, 9, 1))  # 26 days before TODAY
        self.write("plan.md", "2026-08-01", "# Plan")
        self.assertEqual(attention.check("spice", TODAY), [])

    def test_upkeep_line_for_the_agents_prompt(self):
        self.chatted(date(2026, 9, 26))
        self.write("plan.md", "2026-09-04", "# Plan")
        with patch.object(reminders, "now_local", lambda: datetime(2026, 9, 27, 12)):
            line = attention.upkeep_line("spice")
        self.assertIn("plan is out of date (last updated 23 days ago)", line)

    def test_tracking_gap_only_once_the_log_is_in_use(self):
        self.write("plan.md", "2026-09-25", "")
        self.write("backlog.md", "2026-09-25", "")
        table = "| Date | pain |\n|---|---|\n"
        self.write("tracking.md", "2026-09-17", table + "| 17-09-2026 | 3 |\n| 2026-09-16 | 4 |")
        self.assertEqual(attention.check("spice", TODAY), [], "two entries: not in use yet")
        self.write("tracking.md", "2026-09-17", table + "| 17-09-2026 | 3 |\n| 16-09-2026 | 4 |\n| 15-09-2026 | 5 |")
        self.assertEqual(attention.check("spice", TODAY)[0]["text"], "No log entry for 10 days")
        self.write("tracking.md", "2026-09-25", table + "| 25-09-2026 | 3 |\n| 16-09-2026 | 4 |\n| 15-09-2026 | 5 |")
        self.assertEqual(attention.check("spice", TODAY), [])

    def test_digest_lines_one_per_agent(self):
        lines = attention.digest_lines({"spice": [{"text": "A"}, {"text": "B"}], "reba": []})
        self.assertEqual(lines, ["Spice: A (+1 more)"])

    def test_digest_schedule(self):
        monday = datetime(2026, 9, 28, 7, 30)
        weekly = {"digest": "weekly", "digest_time": "07:30"}
        self.assertTrue(attention.is_due(weekly, monday, None))
        self.assertFalse(attention.is_due(weekly, monday, "2026-09-28"), "once a day at most")
        self.assertFalse(attention.is_due(weekly, datetime(2026, 9, 28, 7, 29), None), "not before the time")
        self.assertFalse(attention.is_due(weekly, datetime(2026, 9, 29, 8, 0), None), "weekly = Mondays")
        self.assertTrue(attention.is_due({"digest": "daily", "digest_time": "07:30"}, datetime(2026, 9, 29, 8, 0), None))
        self.assertFalse(attention.is_due({"digest": "off", "digest_time": "07:30"}, monday, None))

    def test_send_digest_pushes_only_when_something_needs_the_owner(self):
        self.write("plan.md", "2026-09-25", "")
        self.write("backlog.md", "2026-09-25", "")
        with patch.object(attention, "check_all", return_value={"spice": []}), \
             patch.object(reminders, "now_local", return_value=datetime(2026, 9, 28, 7, 30)), \
             patch.object(web_push, "send_all", return_value=1) as push:
            self.assertEqual(attention.send_digest()["sent"], 0)
            push.assert_not_called()
            self.assertEqual(attention.state()["last_sent"], "2026-09-28")
            attention.send_digest(force=True)
            push.assert_called_once()

    def test_settings_validate_digest(self):
        with self.assertRaises(ValueError):
            service_settings.update({"digest": "hourly"})
        with self.assertRaises(ValueError):
            service_settings.update({"digest_time": "7:5"})
        self.assertEqual(service_settings.update({"digest": "weekly", "digest_time": "08:15"})["digest"], "weekly")


if __name__ == "__main__":
    unittest.main()
