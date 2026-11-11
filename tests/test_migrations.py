"""Data format version and migrations (migrations.py), and the JSON state
helper they write with (state_io.py)."""
import dataclasses
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import migrations
import state_io
from config import CONFIG


class StateIoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_gives_a_copy_of_the_default(self):
        default = {"items": []}
        loaded = state_io.load_json(self.dir / "nope.json", default)
        loaded["items"].append(1)
        self.assertEqual(default, {"items": []})

    def test_round_trip_and_no_temp_files_left(self):
        path = self.dir / "sub" / "s.json"
        state_io.save_json(path, {"a": "æ"}, ensure_ascii=False)
        self.assertEqual(state_io.load_json(path, {}), {"a": "æ"})
        self.assertEqual(path.read_text(encoding="utf-8"), '{\n  "a": "æ"\n}\n')
        self.assertEqual([p.name for p in path.parent.iterdir()], ["s.json"])

    def test_corrupt_file_is_kept_aside_not_overwritten(self):
        path = self.dir / "s.json"
        path.write_text('{"half": ', encoding="utf-8")
        self.assertEqual(state_io.load_json(path, {"fresh": True}), {"fresh": True})
        self.assertFalse(path.exists())
        self.assertEqual((self.dir / "s.json.corrupt").read_text(), '{"half": ')

    def test_permissions_kept_or_set(self):
        path = self.dir / "s.json"
        path.write_text("{}")
        os.chmod(path, 0o640)
        state_io.save_json(path, {"x": 1})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        state_io.save_json(path, {"x": 2}, mode=0o600)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_failed_write_keeps_the_old_file(self):
        path = self.dir / "s.json"
        state_io.save_json(path, {"old": 1})
        with self.assertRaises(TypeError):
            state_io.save_json(path, {"bad": object()})
        self.assertEqual(state_io.load_json(path), {"old": 1})
        self.assertEqual([p.name for p in self.dir.iterdir()], ["s.json"])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = dataclasses.replace(CONFIG, home=Path(self.tmp.name))
        self.state = self.config.state_dir
        self.lines = []
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_migrations(self, steps):
        return migrations.run(self.config, steps, log=self.lines.append)

    def step(self, name, fail=False):
        def fn(config):
            self.calls.append(name)
            if fail:
                raise RuntimeError("disk on fire")
        return fn

    def test_fresh_install_is_stamped_without_running_anything(self):
        steps = [(1, "baseline", None), (2, "two", self.step(2))]
        result = self.run_migrations(steps)
        self.assertEqual((result["to"], result["ran"], self.calls), (2, [], []))
        self.assertEqual(migrations.stored_version(self.state), 2)
        self.assertFalse((self.state / "backups").exists())

    def test_existing_install_without_version_runs_every_step_once(self):
        self.state.mkdir(parents=True)
        (self.state / "service_settings.json").write_text('{"a": 1}')
        steps = [(1, "baseline", None), (2, "two", self.step(2)), (3, "three", self.step(3))]
        result = self.run_migrations(steps)
        self.assertEqual((result["from"], result["to"], result["ran"]), (None, 3, [1, 2, 3]))
        self.assertEqual(self.calls, [2, 3])
        self.assertEqual(json.loads((self.state / "version.json").read_text()), {"data_version": 3})
        self.assertEqual([line for line in self.lines if not line.startswith("data migration: state files")],
                         ["data migration 1: baseline", "data migration 2: two", "data migration 3: three"])
        # Backed up once, before step 2 (the first that changes data).
        backups = list((self.state / "backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "service_settings.json").read_text(), '{"a": 1}')
        # Second start: nothing left to do.
        again = self.run_migrations(steps)
        self.assertEqual((again["ran"], self.calls), ([], [2, 3]))

    def test_only_steps_above_the_stored_version_run(self):
        self.state.mkdir(parents=True)
        (self.state / "version.json").write_text('{"data_version": 2}')
        self.run_migrations([(1, "baseline", None), (2, "two", self.step(2)), (3, "three", self.step(3))])
        self.assertEqual(self.calls, [3])

    def test_failure_stops_keeps_last_good_version_and_does_not_raise(self):
        self.state.mkdir(parents=True)
        (self.state / "version.json").write_text('{"data_version": 1}')
        steps = [(1, "baseline", None), (2, "two", self.step(2)),
                 (3, "three", self.step(3, fail=True)), (4, "four", self.step(4))]
        result = self.run_migrations(steps)
        self.assertEqual((result["to"], self.calls), (2, [2, 3]))
        self.assertIn("migration 3", result["error"])
        self.assertEqual(migrations.stored_version(self.state), 2)
        self.assertTrue(any("FAILED" in line for line in self.lines))

    def test_baseline_on_an_existing_install_only_stamps(self):
        self.state.mkdir(parents=True)
        (self.state / "reminders.json").write_text("[]")
        result = migrations.run(self.config, log=self.lines.append)
        self.assertEqual((result["to"], result["ran"], result["error"]), (migrations.CURRENT_VERSION, [1], None))
        self.assertFalse((self.state / "backups").exists())

    def test_unwritable_state_folder_does_not_raise(self):
        blocker = Path(self.tmp.name) / "state"
        blocker.write_text("a file where the folder should be")
        result = migrations.run(self.config, log=self.lines.append)
        self.assertIsNotNone(result["error"])


if __name__ == "__main__":
    unittest.main()
