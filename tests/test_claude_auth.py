"""Claude CLI sign-in: a signed-out CLI gives a
clear reply and the amber dot, and the Control Panel can sign in again."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import attention
import claude_auth
import llm_client
from providers import claude_cli

# What Claude CLI 2.1 really prints when it has no login.
SIGNED_OUT = "\n".join(json.dumps(e) for e in [
    {"type": "system", "subtype": "init"},
    {"type": "assistant", "error": "authentication_failed", "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "Not logged in · Please run /login"}]}},
    {"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"},
])

# Stands in for `claude`: `auth login` prints the link and waits for a code
# like the real one; `-p` answers like a signed-in CLI.
FAKE_CLAUDE = """#!/bin/sh
if [ "$1" = "auth" ]; then
  echo "Opening browser to sign in…"
  echo "If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=abc"
  printf "Paste code here if prompted > "
  read code
  if [ "$code" = "good-code#123456" ]; then echo "Login successful."; exit 0; fi
  echo "Invalid code. Please make sure the full code was copied."
  sleep 30
fi
echo '{"type":"result","is_error":false,"result":"ok"}'
"""


class AuthStateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        p = patch.object(claude_auth, "STATE_FILE", self.tmp / "claude_auth.json")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(claude_auth.cancel)


class DetectionTests(AuthStateCase):
    def test_real_signed_out_output_is_recognised(self):
        self.assertTrue(claude_auth.is_auth_failure(SIGNED_OUT))
        self.assertTrue(claude_auth.is_auth_failure("", "OAuth token has expired. Please obtain a new token."))
        self.assertFalse(claude_auth.is_auth_failure('{"type":"assistant","error":"model_not_found"}', "There's an issue with the selected model"))

    def test_agent_gets_a_clear_reply_and_the_state_is_kept(self):
        def fake_run(args, **kwargs):
            kwargs["stdout"].write(SIGNED_OUT.encode())
            return subprocess.CompletedProcess(args, 1, None, "")
        with patch.object(llm_client.subprocess, "run", side_effect=fake_run), \
             patch.object(claude_cli, "_watch_claude_phase", lambda *a: None):
            reply = llm_client._chat_claude_cli("rick", "claude-x", "system", [{"role": "user", "content": "hi"}])
        self.assertEqual(reply, claude_auth.AGENT_REPLY)
        self.assertIn("Claude sign-in", reply)
        self.assertTrue(claude_auth.signed_out())
        self.assertTrue(claude_auth.status()["since"])

    def test_a_working_call_clears_it(self):
        claude_auth.mark_signed_out("x")
        def fake_run(args, **kwargs):
            kwargs["stdout"].write(json.dumps({"type": "result", "is_error": False, "result": "fine"}).encode())
            return subprocess.CompletedProcess(args, 0, None, "")
        with patch.object(llm_client.subprocess, "run", side_effect=fake_run), \
             patch.object(claude_cli, "_watch_claude_phase", lambda *a: None), patch.object(claude_cli, "log_usage"):
            self.assertEqual(llm_client._chat_claude_cli("rick", "claude-x", "system", [{"role": "user", "content": "hi"}]), "fine")
        self.assertFalse(claude_auth.signed_out())
        self.assertTrue(claude_auth.status()["restored"])

    def test_amber_dot_only_for_agents_on_claude_cli(self):
        claude_auth.mark_signed_out("x")
        models = {"rick": "claude-opus-5-5-cli", "stash": "gpt-5.6-sol"}
        with patch.object(attention.llm_models, "selected_model_id", side_effect=lambda a: models[a]), \
             patch("connections.lost_now", return_value=[]), \
             patch.object(attention, "_tracking_finding", return_value=None), \
             patch.object(attention, "_last_chat_day", return_value=None), \
             patch.object(attention.vault, "read_maintainer_inbox", return_value=[]):
            self.assertIn("claude_login", [i["kind"] for i in attention.check("rick")])
            self.assertNotIn("claude_login", [i["kind"] for i in attention.check("stash")])
            claude_auth.mark_ok()
            self.assertNotIn("claude_login", [i["kind"] for i in attention.check("rick")])


class LoginFlowTests(AuthStateCase):
    def setUp(self):
        super().setUp()
        fake = self.tmp / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
        p = patch.object(claude_auth, "_bin", return_value=str(fake))
        p.start()
        self.addCleanup(p.stop)
        claude_auth.mark_signed_out("expired")

    def wait_for(self, *states):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            s = claude_auth.status()
            if s["login"].get("state") in states:
                return s
            time.sleep(0.1)
        self.fail(f"login never reached {states}: {claude_auth.status()}")

    def test_sign_in_restores_access(self):
        s = claude_auth.start_login()
        self.assertEqual(s["login"]["state"], "waiting_code")
        self.assertEqual(s["login"]["url"], "https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=abc")
        self.assertEqual(claude_auth.start_login()["login"]["url"], s["login"]["url"], "a second tap keeps the same link")
        claude_auth.submit_code("good-code#123456")
        s = self.wait_for("done", "failed")
        self.assertEqual(s["login"]["state"], "done", s)
        self.assertFalse(s["signed_out"])
        self.assertNotIn("Login successful", json.dumps(s), "CLI output is never passed on")

    def test_wrong_code_says_so(self):
        claude_auth.start_login()
        claude_auth.submit_code("wrong-code#000000")
        s = self.wait_for("done", "failed")
        self.assertEqual(s["login"]["state"], "failed")
        self.assertIn("didn't accept that code", s["login"]["message"])
        self.assertTrue(s["signed_out"])

    def test_code_is_checked_before_it_reaches_the_cli(self):
        with self.assertRaises(LookupError):
            claude_auth.submit_code("good-code#123456")  # nothing waiting yet
        claude_auth.start_login()
        for bad in ("", "short", "has space#1234567", "line\nbreak#1234567", "x" * 600):
            with self.assertRaises(ValueError):
                claude_auth.submit_code(bad)
        self.assertEqual(claude_auth.status()["login"]["state"], "waiting_code")

    def test_api_key_is_kept_from_the_cli(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test", "DISPLAY": ":99"}):
            env = claude_auth._env()
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("DISPLAY", env)
        self.assertEqual(env["BROWSER"], "/bin/false")


if __name__ == "__main__":
    unittest.main()
