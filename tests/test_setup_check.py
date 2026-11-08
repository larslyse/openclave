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


if __name__ == "__main__":
    unittest.main()
