"""One in-process scheduler for every background job.

A single daemon thread wakes every TICK_S seconds and runs the jobs that are
due. Each feature registers its job once (its module's `start()` does that)
with one of two rules:

- every N seconds, after a first delay: the next run is due N seconds after
  the previous one finished, as the old per-feature `sleep` loops did;
- once, after a delay from registration.

Daily and weekly windows, catch-up after downtime and next-run times stay in
each feature: its job is the feature's own "one pass" function, which decides
by its own state file whether there is anything to do.

Quick jobs run inline on the scheduler thread. Jobs that can take minutes
(model calls, provider probes, a git fetch) run in a small worker pool. A job
never runs twice at once: while it runs, a due tick or a "Run now" skips it.
An exception is recorded on the job and never stops the scheduler.

Jobs marked `background` are switched off when OPENCLAVE_DISABLE_BACKGROUND_JOBS
is set (tests, dev instances and the demo); they stay listed so /api/jobs shows
what exists. Jobs that are part of normal request handling (the chat kicker)
are registered with background=False and always run.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

# The chat kicker polls every 2 seconds (chat_jobs.KICK_POLL_S), the
# shortest interval of any job, so that is the tick.
TICK_S = 2.0
WORKERS = 4


def background_disabled() -> bool:
    return os.environ.get("OPENCLAVE_DISABLE_BACKGROUND_JOBS", "").strip().lower() in {"1", "true", "yes"}


@dataclass
class Job:
    id: str
    name: str
    func: Callable[[], object]
    every: float | None          # None: run once
    delay: float                 # before the first run, from registration
    worker: bool                 # True: runs in the worker pool
    enabled: bool
    next_due: float | None = None  # on the scheduler clock; None: not scheduled
    running: bool = False
    last_start: float | None = None   # wall-clock timestamps for display
    last_finish: float | None = None
    last_result: str | None = None
    runs: int = field(default=0)


def _one_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    line = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return line[:200]


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts is not None else None


class Scheduler:
    """`clock` is monotonic time for due checks; `wall` is real time for the
    timestamps shown to the owner. Tests pass a fake for both."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 tick: float = TICK_S, workers: int = WORKERS):
        self.clock, self.wall, self.tick = clock, wall, tick
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._workers = workers
        self._pool: ThreadPoolExecutor | None = None

    # ── registry ──────────────────────────────────────────────────────────
    def register(self, job_id: str, name: str, func: Callable[[], object], *, every: float | None = None,
                 delay: float = 0, worker: bool = False, background: bool = True) -> Job:
        """Add a job, or replace the one with the same id (its run history
        stays). `every=None` runs it once, `delay` seconds after now."""
        enabled = not (background and background_disabled())
        next_due = self.clock() + delay if enabled else None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                job = self._jobs[job_id] = Job(job_id, name, func, every, delay, worker, enabled, next_due)
            else:  # updated in place: a run in flight finishes on this same record
                job.name, job.func, job.every, job.delay, job.worker, job.enabled = name, func, every, delay, worker, enabled
                if not job.running:
                    job.next_due = next_due
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def jobs(self) -> list[dict]:
        """The job table for /api/jobs, in registration order."""
        now, wall = self.clock(), self.wall()
        with self._lock:
            return [{
                "id": j.id, "name": j.name, "enabled": j.enabled, "running": j.running,
                "last_start": _iso(j.last_start), "last_finish": _iso(j.last_finish),
                "last_result": j.last_result,
                "next_due": _iso(wall + max(0.0, j.next_due - now)) if j.next_due is not None and not j.running else None,
                "every_s": j.every,
            } for j in self._jobs.values()]

    # ── running ───────────────────────────────────────────────────────────
    def _executor(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="openclave-job")
        return self._pool

    def _claim(self, job: Job) -> bool:
        """Mark the job running unless it already is. Caller holds the lock."""
        if job.running:
            return False
        job.running = True
        job.next_due = None
        job.last_start = self.wall()
        return True

    def _execute(self, job: Job) -> None:
        try:
            job.func()
            result = "ok"
        except Exception as exc:  # noqa: BLE001 -- recorded; the scheduler keeps going
            result = _one_line(exc)
            print(f"background job {job.id} failed: {result}")
        with self._lock:
            job.running = False
            job.last_finish = self.wall()
            job.last_result = result
            job.runs += 1
            if job.enabled and job.every is not None:
                job.next_due = self.clock() + job.every

    def _dispatch(self, job: Job, force_worker: bool = False) -> None:
        if job.worker or force_worker:
            self._executor().submit(self._execute, job)
        else:
            self._execute(job)

    def run_pending(self) -> list[str]:
        """One tick: start every due job. Returns the ids started."""
        now = self.clock()
        with self._lock:
            due = [j for j in self._jobs.values()
                   if j.enabled and j.next_due is not None and j.next_due <= now and self._claim(j)]
        for job in due:
            self._dispatch(job)
        return [j.id for j in due]

    def run_now(self, job_id: str) -> tuple[bool, str]:
        """Start one run straight away, in the worker pool so the caller
        never waits. Refused while that job runs or when it's switched off."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False, "no such job"
            if not job.enabled:
                return False, "background jobs are off on this instance"
            if not self._claim(job):
                return False, "already running"
        self._dispatch(job, force_worker=True)
        return True, "started"

    # ── thread ────────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_pending()
            except Exception as exc:  # noqa: BLE001 -- never let the thread die
                print(f"scheduler tick failed: {_one_line(exc)}")
            self._stop.wait(self.tick)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="openclave-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            self._thread = None
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None


SCHEDULER = Scheduler()


def register(job_id: str, name: str, func: Callable[[], object], **rule) -> Job:
    return SCHEDULER.register(job_id, name, func, **rule)


def jobs() -> list[dict]:
    return SCHEDULER.jobs()


def run_now(job_id: str) -> tuple[bool, str]:
    return SCHEDULER.run_now(job_id)


def start() -> None:
    SCHEDULER.start()


def stop() -> None:
    SCHEDULER.stop()
