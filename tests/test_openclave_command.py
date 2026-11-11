"""The `openclave` command (bin/openclave) and updater.py, in a throwaway git
checkout with a scratch OPENCLAVE_HOME. Only the parts that change nothing
outside the temp folder: help, url, status, backup, uninstall without "yes",
update refusing local changes, and the update check against a local
"upstream" repository. Never touches systemd: the unit file path points to a
temp file."""
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import updater

ROOT = Path(__file__).resolve().parent.parent


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
                   cwd=cwd, check=True, capture_output=True, text=True)


def _free_port() -> int:
    for port in range(8990, 8900, -1):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port")


class Checkout(unittest.TestCase):
    """A git repo with the command, config.py and updater.py, plus a
    .venv/bin/python that is this interpreter."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "openclave"
        (self.repo / "bin").mkdir(parents=True)
        for name in ("config.py", "updater.py", "toml_edit.py", "state_io.py"):
            shutil.copy(ROOT / name, self.repo / name)
        shutil.copy(ROOT / "bin" / "openclave", self.repo / "bin" / "openclave")
        (self.repo / "requirements.txt").write_text("")
        (self.repo / ".gitignore").write_text(".venv/\n")
        (self.repo / ".venv" / "bin").mkdir(parents=True)
        (self.repo / ".venv" / "bin" / "python").symlink_to(sys.executable)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "first")
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.port = _free_port()
        (self.data / "openclave.toml").write_text(f'[network]\nport = {self.port}\npublic_url = "https://box.example.ts.net"\n')

    def run_cmd(self, *args, stdin=subprocess.DEVNULL):
        env = {**os.environ, "HOME": str(self.home), "OPENCLAVE_HOME": str(self.data),
               "OPENCLAVE_UNIT_FILE": str(self.tmp / "no-such.service"),
               "OPENCLAVE_BACKUP_DIR": str(self.tmp / "backups")}
        return subprocess.run(["bash", str(self.repo / "bin" / "openclave"), *args], env=env, stdin=stdin,
                              capture_output=True, text=True, timeout=60)


class CommandTests(Checkout):
    def test_help_lists_every_command(self):
        out = self.run_cmd("help").stdout
        for word in ("start", "stop", "restart", "status", "logs", "check", "update", "backup", "uninstall", "url"):
            self.assertIn(f"openclave {word}", out)
        self.assertEqual(self.run_cmd("nonsense").returncode, 2)

    def test_url_reads_the_config(self):
        out = self.run_cmd("url").stdout.splitlines()
        self.assertEqual(out, [f"http://localhost:{self.port}/", "https://box.example.ts.net/",
                               "https://box.example.ts.net/mobile/  (phone app)"])

    def test_status_when_not_running(self):
        result = self.run_cmd("status")
        self.assertEqual(result.returncode, 3)
        self.assertIn("Not running", result.stdout)
        self.assertNotIn("Service:", result.stdout)

    def test_a_unit_for_another_checkout_is_ignored(self):
        unit = self.tmp / "no-such.service"
        unit.write_text("[Service]\nWorkingDirectory=/somewhere/else\n")
        self.assertNotIn("Service:", self.run_cmd("status").stdout)
        self.assertIn("isn't installed as a service", self.run_cmd("logs").stdout)

    def test_backup_leaves_out_caches_and_names_outside_folders(self):
        (self.data / ".env").write_text("ANTHROPIC_API_KEY=test\n")
        (self.data / "agents" / "helper").mkdir(parents=True)
        (self.data / "agents" / "helper" / "mission.md").write_text("hi")
        (self.data / "state" / "qmd-cache").mkdir(parents=True)
        (self.data / "state" / "qmd-cache" / "index.sqlite").write_text("x")
        (self.data / "state" / "usage.json").write_text("{}")
        (self.data / "models" / "voice").mkdir(parents=True)
        (self.data / "models" / "voice" / "big.onnx").write_text("x")
        result = self.run_cmd("backup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("NOT included", result.stdout)
        [archive] = list((self.tmp / "backups").glob("openclave-*.tgz"))
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        with tarfile.open(archive) as tar:
            names = set(tar.getnames())
        self.assertIn("data/.env", names)
        self.assertIn("data/agents/helper/mission.md", names)
        self.assertIn("data/state/usage.json", names)
        self.assertFalse(any("qmd-cache" in n or "big.onnx" in n for n in names), names)

        notes = self.tmp / "notes"
        notes.mkdir()
        with (self.data / "openclave.toml").open("a") as f:
            f.write(f'\n[paths]\nvault_root = "{notes}"\n')
        out = self.run_cmd("backup").stdout
        self.assertIn("NOT included", out)
        self.assertIn(str(notes), out)
        self.assertIn(str(notes / "agents"), out)

    def test_uninstall_without_yes_removes_nothing(self):
        result = self.run_cmd("uninstall")
        self.assertEqual(result.returncode, 0)
        self.assertIn(str(self.repo), result.stdout)
        self.assertIn("delete this folder yourself", result.stdout.lower())
        self.assertIn("Nothing removed", result.stdout)
        self.assertTrue((self.repo / "bin" / "openclave").exists())

    def test_uninstall_refuses_when_data_is_inside_the_checkout(self):
        inside = self.repo / "data"
        inside.mkdir()
        env_data, self.data = self.data, inside
        try:
            result = self.run_cmd("uninstall")
        finally:
            self.data = env_data
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("won't delete", result.stderr)

    def test_update_refuses_local_changes(self):
        (self.repo / "config.py").write_text((self.repo / "config.py").read_text() + "\n# local edit\n")
        result = self.run_cmd("update")
        self.assertEqual(result.returncode, 1)
        self.assertIn("local changes", result.stdout)
        self.assertIn("config.py", result.stdout)


class UpdaterTests(Checkout):
    def setUp(self):
        super().setUp()
        self.upstream = self.tmp / "upstream.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.repo), str(self.upstream)], check=True)
        _git(self.repo, "remote", "add", "origin", str(self.upstream))
        _git(self.repo, "fetch", "-q", "origin")
        _git(self.repo, "branch", "-q", "--set-upstream-to=origin/main", "main")
        root = mock.patch.object(updater, "ROOT", self.repo)
        root.start()
        self.addCleanup(root.stop)
        state = mock.patch.object(updater, "_state_file", return_value=self.tmp / "update_check.json")
        state.start()
        self.addCleanup(state.stop)

    def push_upstream_commit(self, tag=None):
        work = self.tmp / "work"
        subprocess.run(["git", "clone", "-q", str(self.upstream), str(work)], check=True)
        (work / "NEW.md").write_text("new")
        _git(work, "add", ".")
        _git(work, "commit", "-q", "-m", "second")
        if tag:
            _git(work, "tag", tag)
        _git(work, "push", "-q", "origin", "main", "--tags")

    def test_version_prefers_the_version_file(self):
        self.assertEqual(updater.version()["label"], updater.version()["commit"])
        (self.repo / "VERSION").write_text("0.3.0\n")
        self.assertEqual(updater.version()["label"], "0.3.0")

    def test_check_without_tags_compares_with_upstream(self):
        self.assertFalse(updater.check()["available"])
        self.push_upstream_commit()
        result = updater.check()
        self.assertTrue(result["available"])
        self.assertEqual(result["behind"], 1)
        self.assertEqual(updater.cached()["behind"], 1)

    def test_check_with_tags_looks_at_the_latest_tag(self):
        _git(self.repo, "tag", "v0.1.0")
        _git(self.repo, "push", "-q", "origin", "--tags")
        self.assertFalse(updater.check()["available"])
        self.push_upstream_commit(tag="v0.2.0")
        result = updater.check()
        self.assertTrue(result["available"])
        self.assertEqual(result["latest"], "v0.2.0")

    def test_apply_refuses_local_changes(self):
        (self.repo / "config.py").write_text("changed")
        result = updater.apply()
        self.assertFalse(result["ok"])
        self.assertIn("local changes", result["message"])

    def test_apply_pulls_and_installs(self):
        self.push_upstream_commit()
        fake_python = self.tmp / "fake-python"
        fake_python.write_text("#!/bin/sh\necho \"$@\" >> \"$(dirname \"$0\")/pip-calls\"\n")
        fake_python.chmod(0o755)
        with mock.patch.object(updater.sys, "executable", str(fake_python)), \
             mock.patch.object(updater.importlib.util, "find_spec", return_value=None):
            result = updater.apply()
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["changed"])
        self.assertTrue((self.repo / "NEW.md").exists())
        self.assertIn("requirements.txt", (self.tmp / "pip-calls").read_text())
        self.assertNotIn("requirements-voice.txt", (self.tmp / "pip-calls").read_text())

    def test_restart_mode_needs_systemd(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            self.assertEqual(updater.restart_mode(), "")


if __name__ == "__main__":
    unittest.main()
