"""scheduler.py: due rules on a fake clock, failures, worker jobs, Run now,
/api/jobs, and that each feature registers its job with its old cadence."""
import os
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import attention
import chat_jobs
import cleanup
import main
import model_updater
import recaps
import reminders
import scheduled_tasks
import scheduler
import updater


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def wait_idle(job, timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while job.running and time.monotonic() < end:
        time.sleep(0.005)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"OPENCLAVE_DISABLE_BACKGROUND_JOBS": ""})
        env.start()
        self.addCleanup(env.stop)
        self.clock = FakeClock()
        self.s = scheduler.Scheduler(clock=self.clock, wall=lambda: 1_700_000_000.0)
        self.addCleanup(self.s.stop)

    def test_every_rule_fires_after_delay_then_each_interval_after_finish(self):
        calls = []
        self.s.register("a", "A", lambda: calls.append(self.clock()), every=30, delay=10)
        self.assertEqual(self.s.run_pending(), [])
        self.clock.advance(9.9)
        self.assertEqual(self.s.run_pending(), [])
        self.clock.advance(0.1)
        self.assertEqual(self.s.run_pending(), ["a"])
        self.clock.advance(29)
        self.assertEqual(self.s.run_pending(), [])
        self.clock.advance(1)
        self.assertEqual(self.s.run_pending(), ["a"])
        self.assertEqual(len(calls), 2)

    def test_every_without_delay_runs_on_the_first_tick(self):
        self.s.register("a", "A", lambda: None, every=60)
        self.assertEqual(self.s.run_pending(), ["a"])

    def test_once_rule_runs_a_single_time(self):
        calls = []
        self.s.register("o", "Once", lambda: calls.append(1), delay=5)
        self.clock.advance(5)
        self.s.run_pending()
        self.clock.advance(10_000)
        self.s.run_pending()
        self.assertEqual(calls, [1])
        self.assertIsNone(self.s.jobs()[0]["next_due"])

    def test_failing_job_is_recorded_and_the_rest_keep_running(self):
        def boom():
            raise ValueError("disk full\nsecond line")
        ran = []
        self.s.register("bad", "Bad", boom, every=10)
        self.s.register("good", "Good", lambda: ran.append(1), every=10)
        self.s.run_pending()
        self.clock.advance(10)
        self.s.run_pending()
        rows = {r["id"]: r for r in self.s.jobs()}
        self.assertEqual(rows["bad"]["last_result"], "ValueError: disk full")
        self.assertEqual(rows["good"]["last_result"], "ok")
        self.assertEqual(ran, [1, 1])
        self.assertIsNotNone(rows["bad"]["next_due"])  # rescheduled after the failure

    def test_long_worker_job_does_not_block_a_quick_one_or_run_twice(self):
        release, started, quick = threading.Event(), threading.Event(), []
        calls = []

        def slow():
            calls.append(1)
            started.set()
            release.wait(2)

        self.s.register("slow", "Slow", slow, every=5, worker=True)
        self.s.register("quick", "Quick", lambda: quick.append(1), every=5)
        self.s.run_pending()
        self.assertTrue(started.wait(1))
        self.assertEqual(quick, [1])  # ran while slow was still going
        self.clock.advance(60)
        self.assertEqual(self.s.run_pending(), ["quick"])  # slow is still running: skipped
        self.assertEqual(self.s.run_now("slow"), (False, "already running"))
        self.assertTrue(self.s.jobs()[0]["running"])
        release.set()
        wait_idle(self.s.get("slow"))
        self.assertEqual(calls, [1])
        self.assertFalse(self.s.jobs()[0]["running"])
        self.clock.advance(5)
        self.assertIn("slow", self.s.run_pending())

    def test_jobs_disabled_by_env_never_run_but_others_do(self):
        ran = []
        with patch.dict(os.environ, {"OPENCLAVE_DISABLE_BACKGROUND_JOBS": "1"}):
            self.s.register("bg", "Background", lambda: ran.append("bg"), every=1)
            self.s.register("kick", "Kick", lambda: ran.append("kick"), every=1, background=False)
        self.clock.advance(5)
        self.s.run_pending()
        self.assertEqual(ran, ["kick"])
        rows = {r["id"]: r for r in self.s.jobs()}
        self.assertFalse(rows["bg"]["enabled"])
        self.assertTrue(rows["kick"]["enabled"])
        self.assertEqual(self.s.run_now("bg"), (False, "background jobs are off on this instance"))

    def test_run_now_runs_in_the_pool_and_keeps_the_rule(self):
        done = threading.Event()
        self.s.register("j", "J", done.set, every=100, delay=50)
        self.assertEqual(self.s.run_now("j"), (True, "started"))
        self.assertTrue(done.wait(1))
        wait_idle(self.s.get("j"))
        self.assertEqual(self.s.jobs()[0]["last_result"], "ok")
        self.assertEqual(self.s.get("j").next_due, self.clock() + 100)
        self.assertEqual(self.s.run_now("nope"), (False, "no such job"))

    def test_thread_ticks_and_stops(self):
        ran = threading.Event()
        s = scheduler.Scheduler(tick=0.01)
        s.register("t", "T", ran.set, every=60)
        s.start()
        self.assertTrue(ran.wait(1))
        s.stop()
        self.assertIsNone(s._thread)


class JobsApiTests(unittest.TestCase):
    def setUp(self):
        self.s = scheduler.Scheduler(clock=FakeClock(), wall=lambda: 1_700_000_000.0)
        p = patch.object(scheduler, "SCHEDULER", self.s)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.s.stop)
        self.client = TestClient(main.app)

    def test_job_table_shape(self):
        with patch.dict(os.environ, {"OPENCLAVE_DISABLE_BACKGROUND_JOBS": ""}):
            self.s.register("a", "Do A", lambda: None, every=30, delay=60)
        body = self.client.get("/api/jobs").json()
        self.assertEqual(body["jobs"], [{
            "id": "a", "name": "Do A", "enabled": True, "running": False, "last_start": None,
            "last_finish": None, "last_result": None, "next_due": "2023-11-14T22:14:20+00:00", "every_s": 30}])

    def test_run_now_and_refusals(self):
        release = threading.Event()
        with patch.dict(os.environ, {"OPENCLAVE_DISABLE_BACKGROUND_JOBS": ""}):
            self.s.register("a", "Do A", lambda: release.wait(2), every=30, worker=True)
        self.assertEqual(self.client.post("/api/jobs/a/run").json(), {"started": True})
        r = self.client.post("/api/jobs/a/run")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"], "already running")
        release.set()
        wait_idle(self.s.get("a"))
        self.assertEqual(self.client.post("/api/jobs/missing/run").status_code, 404)

    def test_run_now_needs_a_trusted_origin(self):
        self.s.register("a", "Do A", lambda: None, every=30)
        r = self.client.post("/api/jobs/a/run", headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)


class FeatureJobsTests(unittest.TestCase):
    """Each feature's start() registers its job with the cadence its old loop had."""

    def setUp(self):
        env = patch.dict(os.environ, {"OPENCLAVE_DISABLE_BACKGROUND_JOBS": ""})
        env.start()
        self.addCleanup(env.stop)
        self.s = scheduler.Scheduler(clock=FakeClock())
        p = patch.object(scheduler, "SCHEDULER", self.s)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.s.stop)

    def assertJob(self, job_id, func, every, delay, worker):
        job = self.s.get(job_id)
        self.assertIsNotNone(job, job_id)
        self.assertEqual((job.func, job.every, job.delay, job.worker, job.enabled),
                         (func, every, delay, worker, True), job_id)

    def test_reminders(self):
        reminders.start()
        self.assertEqual(reminders.CHECK_EVERY_S, 30)
        self.assertJob("reminders", reminders.run_due_once, reminders.CHECK_EVERY_S, 0, True)

    def test_scheduled_tasks(self):
        scheduled_tasks.start()
        self.assertEqual(scheduled_tasks.CHECK_EVERY_S, 30)
        self.assertJob("scheduled-tasks", scheduled_tasks.run_due_once, scheduled_tasks.CHECK_EVERY_S, 0, False)

    def test_digest(self):
        attention.start()
        self.assertEqual(attention.CHECK_EVERY_S, 60)
        self.assertJob("digest", attention.run_due_once, attention.CHECK_EVERY_S, 0, True)

    def test_weekly_cleanup(self):
        cleanup.start()
        self.assertEqual(cleanup.CHECK_EVERY_S, 300)
        self.assertJob("weekly-cleanup", cleanup.run_due_once, cleanup.CHECK_EVERY_S, 0, True)

    def test_model_updates(self):
        model_updater.start()
        self.assertEqual((model_updater.FIRST_RUN_DELAY_S, model_updater.CHECK_EVERY_S), (15 * 60, 3600))
        self.assertJob("model-updates", model_updater.run_due_once, model_updater.CHECK_EVERY_S,
                       model_updater.FIRST_RUN_DELAY_S, True)

    def test_update_check(self):
        updater.start()
        self.assertEqual((updater.FIRST_LOOK_S, updater.LOOK_EVERY_S, updater.CHECK_EVERY), (300, 1800, 6 * 3600))
        self.assertJob("update-check", updater.run_due_once, updater.LOOK_EVERY_S, updater.FIRST_LOOK_S, True)

    def test_update_check_only_when_on_and_due(self):
        with patch.object(updater, "check") as check, patch.object(updater, "enabled", return_value=False):
            self.assertFalse(updater.run_due_once())
        check.assert_not_called()
        with patch.object(updater, "check") as check, patch.object(updater, "enabled", return_value=True), \
                patch.object(updater, "_due", return_value=True):
            self.assertTrue(updater.run_due_once())
            self.assertFalse(updater.is_checking())
        check.assert_called_once_with()

    def test_recap_backfill_runs_once_and_checks_availability(self):
        recaps.backfill(["a"])
        job = self.s.get("recap-backfill")
        self.assertEqual((job.every, job.delay, job.worker, job.enabled), (None, 0, True, True))
        with patch.object(recaps, "available", return_value=False), patch.object(recaps, "list_for") as listed:
            job.func()
        listed.assert_not_called()

    def test_chat_kicker_runs_even_with_background_jobs_off(self):
        with patch.dict(os.environ, {"OPENCLAVE_DISABLE_BACKGROUND_JOBS": "1"}):
            chat_jobs.start_kicker()
        job = self.s.get("chat-kick")
        self.assertEqual(chat_jobs.KICK_POLL_S, 2)
        self.assertEqual((job.func, job.every, job.delay, job.worker, job.enabled),
                         (chat_jobs.kick_once, chat_jobs.KICK_POLL_S, chat_jobs.KICK_POLL_S, False, True))
        self.assertLessEqual(scheduler.TICK_S, chat_jobs.KICK_POLL_S)

    def test_background_jobs_are_registered_off_when_disabled(self):
        with patch.dict(os.environ, {"OPENCLAVE_DISABLE_BACKGROUND_JOBS": "1"}):
            for start in (reminders.start, scheduled_tasks.start, attention.start, cleanup.start,
                          model_updater.start, updater.start):
                start()
            recaps.backfill([])
        self.assertEqual(len(self.s.jobs()), 7)
        self.assertFalse(any(j["enabled"] for j in self.s.jobs()))


if __name__ == "__main__":
    unittest.main()
