"""[cli] hide_private_files: the CLI subprocesses run under bubblewrap with
private files masked (cli_sandbox.py). The wrapper tests run stand-in
commands (python, sh) through the real bwrap, never the Claude or Codex CLI,
and skip when bwrap can't run here."""
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cli_sandbox  # noqa: E402
import config  # noqa: E402
import llm_runs  # noqa: E402
import vault  # noqa: E402
from providers import claude_cli, codex_cli  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SECRET = "SECRET_SENTINEL"


def _bwrap_works() -> bool:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return False
    try:
        return subprocess.run([bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                               "--unshare-pid", "--die-with-parent", "true"],
                              capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


BWRAP = _bwrap_works()


class SandboxBase(unittest.TestCase):
    """A scratch install: OPENCLAVE_HOME and a vault outside it, a fake home
    folder with credentials, every secret holding SECRET."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.home = root / "home"
        self.oc = self.home / "oc"
        self.vault_dir = root / "vault"
        self.agent = self.vault_dir / "Agents" / "Greg"
        for d in (self.oc / "state" / "garmin", self.vault_dir / "Secret", self.agent,
                  self.home / ".ssh", self.home / "elsewhere"):
            d.mkdir(parents=True)
        shutil.copy(REPO / "tests" / "fixtures" / "Agents" / "Greg" / "agent.json", self.agent / "agent.json")
        (self.oc / ".env").write_text(f"SANDBOX_TEST_KEY={SECRET}\n")
        (self.oc / "private-strings.txt").write_text(SECRET)
        (self.oc / "openclave.toml").write_text(textwrap.dedent(f"""\
            [paths]
            vault_root = "{self.vault_dir}"
            agents_root = "{self.vault_dir / 'Agents'}"
            [privacy]
            confidential_folders = ["Secret"]
            [network]
            access_token = "{SECRET}"
            [cli]
            hide_private_files = true
            """))
        (self.oc / "state" / "push_keys.json").write_text(json.dumps({"private_pem": SECRET}))
        (self.oc / "state" / "usage.json").write_text("{}")
        (self.oc / "state" / "garmin" / "token.json").write_text(SECRET)
        (self.vault_dir / "Secret" / "plan.md").write_text(SECRET)
        (self.vault_dir / "note.md").write_text("an ordinary note")
        (self.home / ".ssh" / "id_ed25519").write_text(SECRET)
        # A symlink in the vault pointing into the confidential folder.
        (self.vault_dir / "shortcut").symlink_to(self.vault_dir / "Secret")

        settings = config.load_config(environ={"OPENCLAVE_HOME": str(self.oc)})
        self.assertTrue(settings.hide_private_files)
        for target, name, value in ((vault, "CONFIG", settings), (vault, "VAULT_ROOT", settings.vault_root),
                                    (vault, "AGENTS_ROOT", settings.agents_root),
                                    (vault, "agent_root", lambda agent_id: self.agent)):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.env = {**os.environ, "HOME": str(self.home), "OPENCLAVE_HOME": str(self.oc),
                    "SANDBOX_TEST_KEY": SECRET}
        self.env.pop("SSH_AUTH_SOCK", None)
        p = patch.dict(os.environ, {"HOME": str(self.home)})
        p.start()
        self.addCleanup(p.stop)

    def run_wrapped(self, argv, cli="codex", **kwargs):
        wrapped = cli_sandbox.wrap(argv, cli=cli, agent_id="greg", env=self.env, **kwargs)
        return subprocess.run(wrapped, env=self.env, capture_output=True, text=True, timeout=60,
                              cwd=REPO, stdin=subprocess.DEVNULL)


PROBE = r"""
import json, sys
out = {}
for name in sys.argv[1:]:
    try:
        with open(name) as f:
            out[name] = "read:" + f.read()
    except OSError as exc:
        out[name] = "error:" + type(exc).__name__
print(json.dumps(out))
"""

WRITE = r"""
import json, sys
out = {}
for name in sys.argv[1:]:
    try:
        with open(name, "w") as f:
            f.write("written")
        out[name] = "ok"
    except OSError as exc:
        out[name] = "error:" + type(exc).__name__
print(json.dumps(out))
"""


@unittest.skipUnless(BWRAP, "bubblewrap can't create a sandbox on this machine")
class WrapperTests(SandboxBase):
    def test_private_files_are_hidden_and_notes_stay_readable(self):
        paths = {
            "env": self.oc / ".env", "strings": self.oc / "private-strings.txt",
            "push": self.oc / "state" / "push_keys.json", "garmin": self.oc / "state" / "garmin" / "token.json",
            "confidential": self.vault_dir / "Secret" / "plan.md", "via_link": self.vault_dir / "shortcut" / "plan.md",
            "ssh": self.home / ".ssh" / "id_ed25519", "toml": self.oc / "openclave.toml",
            "note": self.vault_dir / "note.md", "state": self.oc / "state" / "usage.json",
        }
        result = self.run_wrapped([sys.executable, "-c", PROBE, *map(str, paths.values())])
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = dict(zip(paths, json.loads(result.stdout).values()))
        for name in ("env", "strings", "push", "garmin", "confidential", "via_link", "ssh", "toml"):
            self.assertNotIn(SECRET, seen[name], name)
        self.assertIn("vault_root", seen["toml"])  # the copy without the token
        self.assertEqual(seen["note"], "read:an ordinary note")
        self.assertEqual(seen["state"], "read:{}")

    def test_writes_land_in_the_agent_folder_only(self):
        inside = self.agent / "draft.md"
        elsewhere = self.home / "elsewhere" / "x.md"
        dotfile = self.home / ".bashrc"
        code = REPO / "sandbox-write-test.txt"
        secret_dir = self.vault_dir / "Secret" / "new.md"
        result = self.run_wrapped([sys.executable, "-c", WRITE, *map(str, (inside, elsewhere, dotfile, code, secret_dir))])
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = list(json.loads(result.stdout).values())
        self.assertEqual(seen[0], "ok")
        self.assertEqual(inside.read_text(), "written")
        # Outside the writable set nothing reaches the disk: a read-only
        # error for real paths, a throwaway tmpfs for these temp-dir fakes.
        self.assertEqual(seen[3], "error:OSError")
        for path in (elsewhere, dotfile, code, secret_dir):
            self.assertFalse(path.exists(), path)

    def test_vault_server_imports_its_settings_inside_the_sandbox(self):
        check = ("import config, vault; c = config.CONFIG; "
                 "print(c.vault_root, list(c.confidential_folders), repr(c.access_token), "
                 "sorted(vault.AGENTS), bool(__import__('os').environ.get('SANDBOX_TEST_KEY')))")
        result = self.run_wrapped([sys.executable, "-c", check])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), [str(self.vault_dir), "['Secret']", "''", "['greg']", "True"])

    def test_real_vault_server_lists_its_tools(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        wrapped = cli_sandbox.wrap([sys.executable, llm_runs.MCP_VAULT_SERVER], cli="claude", agent_id="greg", env=self.env)
        proc = subprocess.Popen(wrapped, env={**self.env, "OPENCLAVE_AGENT_ID": "greg"}, cwd=REPO,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            proc.stdin.write("".join(json.dumps(m) + "\n" for m in messages))
            proc.stdin.flush()
            tools = None
            deadline = time.time() + 60
            while time.time() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                reply = json.loads(line)
                if reply.get("id") == 2:
                    tools = [t["name"] for t in reply["result"]["tools"]]
                    break
        finally:
            proc.stdin.close()
            proc.kill()
            stderr = proc.stderr.read()
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()
        self.assertTrue(tools, stderr[-2000:])
        self.assertIn("read_vault_file", tools)

    def test_cancel_running_stops_the_whole_tree(self):
        env = dict(self.env)
        marker = llm_runs._mark_run("greg", env)
        self.addCleanup(llm_runs._unmark_run, "greg", marker)
        wrapped = cli_sandbox.wrap(["sh", "-c", "sleep 300 & sleep 300 & echo started; wait"],
                                   cli="codex", agent_id="greg", env=env)
        proc = subprocess.Popen(wrapped, env=env, stdout=subprocess.PIPE, text=True,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertEqual(proc.stdout.readline().strip(), "started")
        needle = f"OPENCLAVE_RUN={marker}".encode()

        def marked():
            pids = []
            for d in Path("/proc").iterdir():
                try:
                    if d.name.isdigit() and needle in (d / "environ").read_bytes().split(b"\0"):
                        pids.append(int(d.name))
                except OSError:
                    continue
            return pids

        self.assertGreaterEqual(len(marked()), 4)  # bwrap, its init, sh, two sleeps
        self.assertTrue(llm_runs.cancel_running("greg"))
        proc.wait(timeout=10)
        deadline = time.time() + 10
        while marked() and time.time() < deadline:
            time.sleep(0.1)
        self.assertEqual(marked(), [])

    def test_nested_bwrap_works_inside_the_wrapper(self):
        # Codex starts its own bwrap sandbox inside ours.
        result = self.run_wrapped(["bwrap", "--unshare-all", "--share-net", "--ro-bind", "/", "/",
                                   "--dev", "/dev", "--proc", "/proc", "--die-with-parent", "true"])
        self.assertEqual(result.returncode, 0, result.stderr)


class SettingTests(SandboxBase):
    def test_off_by_default(self):
        self.assertFalse(config.Settings.__dataclass_fields__["hide_private_files"].default)
        self.assertFalse(config.load_config(environ={"OPENCLAVE_HOME": str(self.home / "none")}).hide_private_files)

    def test_rejects_non_boolean(self):
        (self.oc / "openclave.toml").write_text('[cli]\nhide_private_files = "yes"\n')
        with self.assertRaises(ValueError):
            config.load_config(environ={"OPENCLAVE_HOME": str(self.oc)})

    def test_mask_list(self):
        masks = cli_sandbox.private_paths("codex", self.env)
        for path in (self.oc / ".env", self.oc / "private-strings.txt", self.oc / "state" / "push_keys.json",
                     self.oc / "state" / "garmin", self.vault_dir / "Secret", self.home / ".ssh"):
            self.assertIn(path, masks)
        self.assertNotIn(self.oc / "state", masks)
        self.assertNotIn(self.home / ".aws", masks)  # only paths that exist

    def test_other_clis_login_is_masked(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / ".credentials.json").write_text(SECRET)
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "auth.json").write_text(SECRET)
        codex_masks = cli_sandbox.private_paths("codex", self.env)
        self.assertIn(self.home / ".claude" / ".credentials.json", codex_masks)
        self.assertNotIn(self.home / ".codex" / "auth.json", codex_masks)
        self.assertIn(self.home / ".codex" / "auth.json", cli_sandbox.private_paths("claude", self.env))

    def test_env_names_forwarded_to_the_vault_server(self):
        self.assertEqual(cli_sandbox.env_file_names(self.env), ["OPENCLAVE_HOME", "SANDBOX_TEST_KEY"])

    def test_token_that_cannot_be_removed_fails_closed(self):
        (self.oc / "openclave.toml").write_text(f'network = {{ access_token = "{SECRET}" }}\n')
        with patch.object(cli_sandbox, "_check_bwrap", return_value="/usr/bin/bwrap"), \
                self.assertRaises(cli_sandbox.SandboxUnavailable):
            cli_sandbox.wrap(["true"], cli="codex", agent_id="greg", env=self.env)

    def test_writable_path_inside_a_masked_one_fails_closed(self):
        with patch.object(cli_sandbox, "_check_bwrap", return_value="/usr/bin/bwrap"), \
                self.assertRaises(cli_sandbox.SandboxUnavailable):
            cli_sandbox.wrap(["true"], cli="codex", agent_id="greg", env=self.env,
                             writable=[self.vault_dir / "Secret"])

    def test_no_bwrap_fails_closed_in_both_providers(self):
        def no_run(*args, **kwargs):
            raise AssertionError("the CLI must not run unwrapped")

        with patch.object(cli_sandbox.shutil, "which", return_value=None), \
                patch.object(subprocess, "run", side_effect=no_run), \
                patch.object(claude_cli, "_has_web_search", return_value=False), \
                patch.object(claude_cli, "_extra_servers", return_value={}), \
                patch.object(codex_cli, "_has_web_search", return_value=False):
            claude_reply = claude_cli._chat_claude_cli("greg", "m", "sys", [{"role": "user", "content": "hi"}])
            codex_reply = codex_cli._chat_codex_cli("greg", "m", "sys", [{"role": "user", "content": "hi"}])
        self.assertEqual(claude_reply, f"({cli_sandbox.NO_BWRAP})")
        self.assertEqual(codex_reply, f"({cli_sandbox.NO_BWRAP})")

    def test_off_leaves_the_command_alone(self):
        calls = []
        off = dataclasses.replace(vault.CONFIG, hide_private_files=False)
        with patch.object(vault, "CONFIG", off), \
                patch.object(cli_sandbox, "wrap", side_effect=AssertionError("wrapped while off")), \
                patch.object(subprocess, "run", side_effect=lambda argv, **kw: calls.append(argv) or
                             subprocess.CompletedProcess(argv, 1, "", "")), \
                patch.object(claude_cli, "_has_web_search", return_value=False), \
                patch.object(claude_cli, "_extra_servers", return_value={}):
            claude_cli._chat_claude_cli("greg", "m", "sys", [{"role": "user", "content": "hi"}])
        self.assertEqual(calls[0][0], claude_cli.CLAUDE_BIN)


if __name__ == "__main__":
    unittest.main()
