"""First-run setup (README): which model routes are ready, keys from
OPENCLAVE_HOME/.env, and a plain message when a provider refuses the key."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import setup_check


class SetupCheckTests(unittest.TestCase):
    def test_api_routes_follow_their_keys(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}, clear=False), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XAI_API_KEY", None)
            ready = setup_check.ready_providers()
        self.assertIn("anthropic", ready)
        self.assertNotIn("xai", ready)

    def test_no_route_means_not_ready(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(setup_check, "_which", return_value=None):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("XAI_API_KEY", None)
            summary = setup_check.summary()
        self.assertFalse(summary["ready"])
        self.assertTrue(all(r["fix"] for r in summary["routes"]))


class EnvFileTests(unittest.TestCase):
    def test_reads_keys_without_overriding_the_environment(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / ".env"
            f.write_text('# keys\nexport OC_TEST_A="one"\nOC_TEST_B=two\nOC_TEST_EMPTY=\nOC_TEST_SET=file\nnot a line\n')
            with mock.patch.dict(os.environ, {"OC_TEST_SET": "shell"}, clear=False):
                config.load_env_file(f)
                self.assertEqual(os.environ["OC_TEST_A"], "one")
                self.assertEqual(os.environ["OC_TEST_B"], "two")
                self.assertNotIn("OC_TEST_EMPTY", os.environ)
                self.assertEqual(os.environ["OC_TEST_SET"], "shell")
                for k in ("OC_TEST_A", "OC_TEST_B"):
                    os.environ.pop(k, None)

    def test_missing_file_is_fine(self):
        config.load_env_file(Path("/nonexistent/.env"))


class RefusedKeyTests(unittest.TestCase):
    def test_auth_error_says_where_the_key_goes(self):
        import chat_service
        import llm_models
        with mock.patch.object(llm_models, "selected_model_id", return_value="claude-sonnet-5"):
            msg = chat_service._provider_failure("any", "Error code: 401 - {'type': 'authentication_error'}")
        self.assertIn("ANTHROPIC_API_KEY", msg)
        self.assertIn(".env", msg)


class OpenNetworkWarningTests(unittest.TestCase):
    def _with(self, bind, token=""):
        import dataclasses
        return mock.patch.object(setup_check, "CONFIG",
                                 dataclasses.replace(setup_check.CONFIG, bind_address=bind, access_token=token))

    def row(self):
        return next(x for x in setup_check.extras() if x["id"] == "network")

    def test_loopback_is_fine(self):
        for bind in ("127.0.0.1", "localhost", "::1", "127.0.0.2"):
            with self._with(bind):
                self.assertEqual(setup_check.open_network_warning(), "")
                self.assertTrue(self.row()["ready"])

    def test_open_bind_without_token_warns_but_is_optional(self):
        for bind in ("0.0.0.0", "::", "192.168.1.5"):
            with self._with(bind):
                self.assertIn("access_token", setup_check.open_network_warning())
                row = self.row()
                self.assertFalse(row["ready"])
                self.assertIn("access_token", row["fix"])
                self.assertNotIn("network", [r["id"] for r in setup_check.routes()])

    def test_token_silences_the_warning(self):
        with self._with("0.0.0.0", "tok"):
            self.assertEqual(setup_check.open_network_warning(), "")
            self.assertTrue(self.row()["ready"])


class SaveKeyTests(unittest.TestCase):
    """Control Panel → Setup → Add an API key (POST /api/setup/key). The
    provider check is mocked: no real provider calls."""
    KEY = "xai-" + "a1B2" * 10

    def setUp(self):
        import dataclasses
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cfg = mock.patch.object(setup_check, "CONFIG", dataclasses.replace(setup_check.CONFIG, home=self.home))
        self.cfg.start()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("XAI_API_KEY", None)

    def tearDown(self):
        self.env.stop()
        self.cfg.stop()
        self.tmp.cleanup()

    def test_accepted_key_is_saved_private_and_live(self):
        (self.home / ".env").write_text("OTHER=1\nexport XAI_API_KEY=old\n")
        with mock.patch.object(setup_check, "check_key", return_value="accepted"), \
             mock.patch.object(setup_check, "_give_agents_a_ready_model", return_value=[]):
            r = setup_check.save_key("xai", self.KEY)
        self.assertTrue(r["saved"])
        self.assertNotIn(self.KEY, str(r), "the key is never echoed back")
        self.assertEqual((self.home / ".env").read_text(), f"OTHER=1\nXAI_API_KEY={self.KEY}\n")
        self.assertEqual((self.home / ".env").stat().st_mode & 0o777, 0o600)
        self.assertEqual(os.environ["XAI_API_KEY"], self.KEY)
        self.assertIn("xai", setup_check.ready_providers())

    def test_refused_key_is_not_saved(self):
        with mock.patch.object(setup_check, "check_key", return_value="refused"):
            r = setup_check.save_key("xai", self.KEY)
        self.assertFalse(r["saved"])
        self.assertFalse((self.home / ".env").exists())
        self.assertNotIn("XAI_API_KEY", os.environ)

    def test_unchecked_key_is_saved_with_a_note(self):
        with mock.patch.object(setup_check, "check_key", return_value="unknown"), \
             mock.patch.object(setup_check, "_give_agents_a_ready_model", return_value=[]):
            r = setup_check.save_key("xai", self.KEY)
        self.assertTrue(r["saved"])
        self.assertIn("Couldn't reach", r["message"])

    def test_implausible_input_is_rejected_before_any_check(self):
        with mock.patch.object(setup_check, "check_key") as check:
            for bad in ("", "short", self.KEY + "\nEVIL=1", "x" * 301, "key with spaces in it here"):
                with self.assertRaises(ValueError):
                    setup_check.save_key("xai", bad)
            with self.assertRaises(ValueError):
                setup_check.save_key("openai", self.KEY)
        check.assert_not_called()

    def test_check_maps_status_codes(self):
        for code, want in ((200, "accepted"), (401, "refused"), (403, "refused"), (500, "unknown")):
            with mock.patch.object(setup_check.httpx, "get", return_value=mock.Mock(status_code=code)):
                self.assertEqual(setup_check.check_key("anthropic", self.KEY), want)
        with mock.patch.object(setup_check.httpx, "get", side_effect=setup_check.httpx.ConnectError("down")):
            self.assertEqual(setup_check.check_key("xai", self.KEY), "unknown")

    def test_route_never_echoes_the_key(self):
        from fastapi.testclient import TestClient
        import main
        client = TestClient(main.app)
        for body in (f'"{self.KEY}"', f'{{"provider": "xai", "key": "{self.KEY}\\nX=1"}}', "not json " + self.KEY):
            r = client.post("/api/setup/key", content=body, headers={"Content-Type": "application/json"})
            self.assertEqual(r.status_code, 400)
            self.assertNotIn(self.KEY, r.text)
        self.assertEqual(client.get("/api/setup/key").status_code, 405, "POST only")

    def test_agents_without_a_usable_model_get_a_ready_one(self):
        import agents
        import llm_models
        some = next(iter(agents.AGENTS))
        chosen = {}
        os.environ["XAI_API_KEY"] = self.KEY
        os.environ.pop("ANTHROPIC_API_KEY", None)
        with mock.patch.dict(agents.AGENTS, {some: agents.AGENTS[some]}, clear=True), \
             mock.patch.object(setup_check, "_which", return_value=None), \
             mock.patch.object(llm_models, "selected_model_id", return_value="claude-sonnet-4-6"), \
             mock.patch.object(llm_models, "set_selected_model", side_effect=lambda a, m: chosen.update({a: m})):
            moved = setup_check._give_agents_a_ready_model()
        self.assertEqual(moved, [agents.AGENTS[some].name])
        self.assertEqual(llm_models.get(chosen[some]).provider, "xai")


class FirstRunTests(unittest.TestCase):
    def test_suggests_a_few_ready_made_agents_but_not_the_manager(self):
        import catalog
        d = setup_check.first_run()
        self.assertLessEqual(len(d["suggested"]), setup_check.SUGGESTED_AGENTS)
        managers = {t["id"] for t in catalog.templates() if t["manager"]}
        self.assertFalse(managers & {s["id"] for s in d["suggested"]})

    def test_claude_cli_without_a_login_is_not_ready(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}, clear=False), \
             mock.patch.object(setup_check.sys, "platform", "linux"), \
             mock.patch.object(setup_check, "_which", return_value="/usr/bin/claude"):
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            row = next(r for r in setup_check.routes() if r["id"] == "claude-cli")
            self.assertFalse(row["ready"])
            self.assertTrue(row["needs_sign_in"])
            (Path(d) / ".credentials.json").write_text("{}")
            row = next(r for r in setup_check.routes() if r["id"] == "claude-cli")
            self.assertTrue(row["ready"])

    def test_manager_onboarding_states_the_real_review_schedule(self):
        import cleanup
        text = (Path(setup_check.__file__).parent / "catalog" / "agents" / "rick" / "onboarding.md").read_text()
        self.assertIn(f"{cleanup.RUN_HOUR:02d}:00", text)
        self.assertIn(f"{cleanup.LAST_HOUR:02d}:00", text)
        self.assertEqual(cleanup.RUN_WEEKDAY, 0, "the text says Monday")
        self.assertIn("Monday", text)


if __name__ == "__main__":
    unittest.main()
