import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agents
import connections
import llm_client


class AgentConfigDataTests(unittest.TestCase):
    def test_agent_owned_cli_mcp_voice_and_role_settings_load(self):
        cfg = agents.AGENTS["rick"]
        self.assertEqual(cfg.cli_tools, ("WebSearch", "WebFetch"))
        self.assertEqual(cfg.voice["provider"], "fish_audio")
        self.assertEqual(agents.AGENTS["r2"].mcp_servers.keys(), {"ha", "ha-admin"})
        self.assertEqual(connections.needed("reba"), ["Garmin"])

    def test_mcp_environment_placeholders_expand_without_mutating_agent_data(self):
        cfg = agents.AGENTS["r2"]
        with patch.dict("os.environ", {"HA_API_TOKEN": "test-token"}):
            servers = llm_client._extra_servers("r2")
        self.assertEqual(servers["ha"]["headers"]["Authorization"], "Bearer test-token")
        self.assertEqual(servers["ha-admin"]["env"]["HA_API_TOKEN"], "test-token")
        self.assertEqual(cfg.mcp_servers["ha-admin"]["env"]["HA_API_TOKEN"], "${HA_API_TOKEN}")

    def test_registry_loads_folder_data_without_a_builtin_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "agents"
            folder = root / "Custom"
            folder.mkdir(parents=True)
            (folder / "agent.json").write_text(json.dumps({
                "id": "custom", "name": "Custom", "color": "#123456", "tagline": "Test",
                "roles": ["test-role"], "cli_tools": ["WebSearch"],
                "mcp_servers": {"demo": {"command": "demo"}},
            }), encoding="utf-8")
            with patch.object(agents, "AGENTS_ROOT", root), patch.dict(agents.AGENTS):
                agents._load_created_agents()
                self.assertEqual(agents.AGENTS["custom"].roles, ("test-role",))
                self.assertEqual(agents.AGENTS["custom"].folder, "Custom")

    def test_maintainer_capability_follows_role_not_agent_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "agents"
            folder = root / "Operator"
            folder.mkdir(parents=True)
            (folder / "agent.json").write_text(json.dumps({
                "id": "operator", "name": "Operator", "color": "#123456", "tagline": "Test",
                "roles": ["maintainer"],
                "connections": {"home_assistant": {
                    "label": "Building Controls", "short": "BC",
                    "routes": ["codex-cli"], "mcp_servers": ["controls-admin"],
                }},
            }), encoding="utf-8")
            with patch.object(agents, "AGENTS_ROOT", root), patch.dict(agents.AGENTS):
                agents._load_created_agents()
                self.assertTrue(agents.has_role("operator", "maintainer"))
                self.assertEqual(agents.maintainer_id(), "operator")
                self.assertTrue(agents.is_home_assistant_agent("operator"))
                self.assertEqual(connections.needed("operator"), ["Building Controls"])
                self.assertEqual(connections.supported("operator", "gpt-6-sol"), ["BC"])


if __name__ == "__main__":
    unittest.main()
