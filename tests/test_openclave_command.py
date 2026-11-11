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


class UpstreamCheckout(Checkout):
    """The checkout cloned from a local bare "upstream", on main tracking
    origin/main, with updater.py pointed at it."""

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
        self.channel = "release"
        chan = mock.patch.object(updater, "channel", side_effect=lambda: self.channel)
        chan.start()
        self.addCleanup(chan.stop)


class UpdaterTests(UpstreamCheckout):
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

    def test_version_names_the_release_tag(self):
        self.assertEqual(updater.version()["tag"], "")
        _git(self.repo, "tag", "v0.1.0")
        self.assertEqual(updater.version()["tag"], "v0.1.0")

    def test_restart_mode_needs_systemd(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            self.assertEqual(updater.restart_mode(), "")


class ChannelTests(UpstreamCheckout):
    """The channel rule: releases (vX.Y.Z tags) by default, the branch while
    there are none or with channel "main", and never backwards."""

    def setUp(self):
        super().setUp()
        self.work = self.tmp / "maintainer"
        subprocess.run(["git", "clone", "-q", str(self.upstream), str(self.work)], check=True)

    def publish(self, name, tag=None):
        """The maintainer pushes one commit (and maybe a tag) upstream."""
        (self.work / f"{name}.md").write_text(name)
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-q", "-m", name)
        if tag:
            _git(self.work, "tag", "-a", tag, "-m", tag)
        _git(self.work, "push", "-q", "origin", "main", "--tags")
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.work, capture_output=True,
                              text=True, check=True).stdout.strip()

    def head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True,
                              text=True, check=True).stdout.strip()

    def branch(self):
        return subprocess.run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=self.repo,
                              capture_output=True, text=True).stdout.strip()

    def test_without_tags_it_follows_the_branch(self):
        tip = self.publish("a")
        result = updater.apply(packages=False)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["changed"])
        self.assertEqual(self.head(), tip)
        self.assertEqual(self.branch(), "main")

    def test_with_tags_it_moves_to_the_newest_release_not_the_tip(self):
        self.publish("a", tag="v0.9.0")
        release = self.publish("b", tag="v0.10.0")
        self.publish("c")  # unreleased work on main
        check = updater.check()
        self.assertTrue(check["available"])
        self.assertEqual(check["latest"], "v0.10.0")  # version order, not text order
        self.assertEqual(check["behind"], 2)
        result = updater.apply(packages=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.head(), release)
        self.assertEqual(self.branch(), "")  # detached on the tag
        self.assertEqual(updater.version()["tag"], "v0.10.0")
        self.assertEqual(updater.apply(packages=False)["message"], "Already up to date.")

    def test_it_never_moves_backwards(self):
        # An install that followed main is ahead of the first release.
        self.publish("a", tag="v0.1.0")
        tip = self.publish("b")
        _git(self.repo, "pull", "-q", "--ff-only")  # what updates did before channels
        self.assertEqual(self.head(), tip)
        check = updater.check()
        self.assertFalse(check["available"])
        self.assertTrue(check["ahead"])
        self.assertIn("ahead of the latest release", check["summary"])
        result = updater.apply(packages=False)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["changed"])
        self.assertIn("ahead of the latest release", result["message"])
        self.assertEqual(self.head(), tip)
        self.assertEqual(self.branch(), "main")
        # A newer release on top of it is taken.
        release = self.publish("c", tag="v0.2.0")
        self.assertTrue(updater.apply(packages=False)["changed"])
        self.assertEqual(self.head(), release)

    def test_channel_main_ignores_tags(self):
        self.channel = "main"
        self.publish("a", tag="v0.1.0")
        tip = self.publish("b")
        check = updater.check()
        self.assertEqual(check["channel"], "main")
        self.assertEqual(check["behind"], 2)
        self.assertTrue(updater.apply(packages=False)["ok"])
        self.assertEqual(self.head(), tip)
        self.assertEqual(self.branch(), "main")

    def test_detached_release_updates_to_the_next_release(self):
        self.publish("a", tag="v0.1.0")
        _git(self.repo, "fetch", "-q", "--tags", "origin")
        _git(self.repo, "checkout", "-q", "--detach", "v0.1.0")
        self.assertFalse(updater.check()["available"])
        release = self.publish("b", tag="v0.2.0")
        result = updater.apply(packages=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.head(), release)
        self.assertEqual(self.branch(), "")

    def test_detached_release_switched_to_channel_main_goes_back_onto_the_branch(self):
        self.publish("a", tag="v0.1.0")
        _git(self.repo, "fetch", "-q", "--tags", "origin")
        _git(self.repo, "checkout", "-q", "--detach", "v0.1.0")
        tip = self.publish("b")
        self.channel = "main"
        result = updater.apply(packages=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.head(), tip)
        self.assertEqual(self.branch(), "main")

    def test_detached_head_refuses_local_changes(self):
        self.publish("a", tag="v0.1.0")
        _git(self.repo, "fetch", "-q", "--tags", "origin")
        _git(self.repo, "checkout", "-q", "--detach", "v0.1.0")
        self.publish("b", tag="v0.2.0")
        (self.repo / "config.py").write_text("changed")
        before = self.head()
        result = updater.apply(packages=False)
        self.assertFalse(result["ok"])
        self.assertIn("local changes", result["message"])
        self.assertEqual(self.head(), before)
        self.assertEqual(updater.local_changes(), ["config.py"])

    def test_first_checkout_of_a_fresh_clone(self):
        self.publish("a", tag="v0.1.0")
        self.publish("b")
        clone = self.tmp / "fresh"
        subprocess.run(["git", "clone", "-q", str(self.upstream), str(clone)], check=True)
        with mock.patch.object(updater, "ROOT", clone):
            self.assertTrue(updater.first_checkout()["ok"])
            self.assertEqual(updater.version()["tag"], "v0.1.0")
        # Without tags it stays on the branch.
        bare = self.tmp / "notags.git"
        subprocess.run(["git", "clone", "-q", "--bare", "--no-tags", str(self.repo), str(bare)], check=True)
        clone2 = self.tmp / "fresh2"
        subprocess.run(["git", "clone", "-q", str(bare), str(clone2)], check=True)
        with mock.patch.object(updater, "ROOT", clone2):
            result = updater.first_checkout()
            self.assertTrue(result["ok"], result)
            self.assertIn("no release yet", result["message"])
            self.assertEqual(subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=clone2,
                                            capture_output=True, text=True).stdout.strip(), "main")


if __name__ == "__main__":
    unittest.main()
