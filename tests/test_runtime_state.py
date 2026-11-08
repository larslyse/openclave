"""Runtime state and custom avatars stay under the configured data directory."""
import unittest

import agent_factory
import agent_order
import agent_tools
import attention
import claude_auth
import cleanup
import ha_guard
import llm_client
import llm_models
import main
import model_discovery
import service_settings
import web_push
from config import CONFIG


class RuntimeStatePathTests(unittest.TestCase):
    def test_state_files_and_custom_avatars_live_under_openclave_home(self):
        files = (
            agent_order.ORDER_FILE,
            agent_tools.SELECTION_FILE,
            attention.STATE_FILE,
            claude_auth.STATE_FILE,
            cleanup.STATE_FILE,
            ha_guard.STATE_FILE,
            llm_client.USAGE_LOG,
            llm_client.CLI_QUOTA_FILE,
            llm_models.SELECTION_FILE,
            llm_models.AUTO_FILE,
            model_discovery.CHECK_FILE,
            service_settings.SETTINGS_FILE,
            web_push.KEYS_FILE,
            web_push.SUBSCRIPTIONS_FILE,
        )
        for path in files:
            with self.subTest(path=path.name):
                self.assertEqual(path.parent, CONFIG.state_dir)
        self.assertEqual(agent_factory.AVATAR_DIR, CONFIG.state_dir / "custom_avatars")
        self.assertEqual(main.AVATAR_DIR, CONFIG.state_dir / "custom_avatars")


if __name__ == "__main__":
    unittest.main()
