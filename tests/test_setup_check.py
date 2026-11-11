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


if __name__ == "__main__":
    unittest.main()
