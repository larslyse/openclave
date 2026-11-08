import io
import json
import shutil
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import import_legacy  # noqa: E402

OLD_AGENTS = '''
from dataclasses import dataclass
@dataclass(frozen=True)
class AgentConfig:
    id: str
    name: str
    folder: str
    color: str
    tagline: str
ALPHA = AgentConfig(id="alpha", name="Alpha", folder="Alpha", color="#111111", tagline="Home things")
RICK = AgentConfig(id="rick", name="Rick", folder="Rick", color="#222222", tagline="Maintains the app")
ROCKY = AgentConfig(id="rocky", name="Rocky", folder="Rocky", color="#333333", tagline="Builds")
'''
OLD_LLM = '''
import os
AGENT_EXTRA_MCP_SERVERS: dict[str, dict] = {
    "alpha": {
        "ha": {"type": "sse", "url": "http://ha.invalid:8123/mcp_server/sse",
               "headers": {"Authorization": f"Bearer {os.environ.get('HA_API_TOKEN', '')}"}},
        "ha-admin": {"command": "/usr/bin/python3", "args": ["x/mcp_ha_server.py"],
                     "env": {"HA_API_TOKEN": os.environ.get("HA_API_TOKEN", "")}},
    },
}
AGENT_TOOLS: dict[str, str] = {"rick": "WebSearch,WebFetch"}
'''
OLD_CHAT = 'AGENT_VOICE = {"rick": {"provider": "fish_audio", "voice_id": "abc"}}\n'
OLD_HA = 'import os\nHA_BASE_URL = os.environ.get("HA_BASE_URL", "http://ha.invalid:8123")\n'


class ImportLegacyTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        self.old, self.agents, self.home = tmp / "old", tmp / "Agents", tmp / "home"
        (self.old / "static" / "avatars").mkdir(parents=True)
        (self.old / "static" / "custom_avatars").mkdir()
        for name, text in (("agents.py", OLD_AGENTS), ("llm_client.py", OLD_LLM), ("chat_service.py", OLD_CHAT),
                           ("mcp_ha_server.py", OLD_HA)):
            (self.old / name).write_text(textwrap.dedent(text))
        (self.old / "static" / "avatars" / "alpha.svg").write_text("<svg/>")
        (self.old / "static" / "custom_avatars" / "beta.svg").write_text("<svg/>")
        (self.old / "usage_log.jsonl").write_text("{}\n")
        for folder in ("Alpha", "Rick", "Rocky", "Beta"):
            (self.agents / folder).mkdir(parents=True)
        (self.agents / "Alpha" / "agent.json").write_text(json.dumps({"id": "alpha", "name": "Alfa", "tagline": "Renamed"}))
        (self.agents / "Beta" / "agent.json").write_text(json.dumps(
            {"id": "beta", "name": "Beta", "color": "#444444", "tagline": "UI-created"}))

    def run_import(self, apply: bool) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            changes = import_legacy.run(self.old, self.agents, self.home, apply=apply)
        return changes, out.getvalue()

    def load(self, folder: str) -> dict:
        return json.loads((self.agents / folder / "agent.json").read_text())

    def test_dry_run_writes_nothing(self):
        before = (self.agents / "Alpha" / "agent.json").read_text()
        changes, out = self.run_import(apply=False)
        self.assertGreater(changes, 0)
        self.assertIn("dry run", out)
        self.assertEqual((self.agents / "Alpha" / "agent.json").read_text(), before)
        self.assertFalse((self.agents / "Rick" / "agent.json").exists())
        self.assertFalse(self.home.exists())

    def test_apply_turns_legacy_tables_into_agent_data(self):
        self.run_import(apply=True)
        alpha = self.load("Alpha")
        self.assertEqual((alpha["name"], alpha["tagline"], alpha["color"]), ("Alfa", "Renamed", "#111111"),
                         "existing fields win; gaps filled from the old code")
        self.assertEqual(alpha["mcp_servers"]["ha"]["url"], "http://ha.invalid:8123/mcp_server/sse")
        self.assertEqual(alpha["mcp_servers"]["ha"]["headers"]["Authorization"], "Bearer ${HA_API_TOKEN}")
        admin = alpha["mcp_servers"]["ha-admin"]
        self.assertEqual(admin["args"], ["${OPENCLAVE_DIR}/mcp_ha_server.py"])
        self.assertEqual(admin["env"]["HA_BASE_URL"], "http://ha.invalid:8123")
        self.assertEqual(alpha["connections"]["home_assistant"]["mcp_servers"], ["ha", "ha-admin"])
        rick = self.load("Rick")
        self.assertEqual(rick["roles"], ["maintainer"])
        self.assertEqual(rick["cli_tools"], ["WebSearch", "WebFetch"])
        self.assertEqual(rick["voice"], {"provider": "fish_audio", "voice_id": "abc"})
        self.assertEqual(self.load("Beta")["tagline"], "UI-created")
        self.assertEqual(json.loads((self.agents / "Rocky" / "page.json").read_text())["blocks"][0]["file"], "plan.md")
        self.assertTrue((self.agents / "Alpha" / "avatar.svg").exists())
        self.assertTrue((self.home / "state" / "custom_avatars" / "beta.svg").exists())
        self.assertTrue((self.home / "state" / "usage_log.jsonl").exists())
        self.assertIn(f'agents_root = "{self.agents}"', (self.home / "openclave.toml").read_text())

    def test_second_run_changes_nothing(self):
        self.run_import(apply=True)
        changes, _ = self.run_import(apply=True)
        self.assertEqual(changes, 0)

    def test_imported_agents_load_in_the_new_registry(self):
        import agents
        self.run_import(apply=True)
        configs = {}
        for path in self.agents.glob("*/agent.json"):
            cfg = agents._agent_config(json.loads(path.read_text()), path.parent.name)
            configs[cfg.id] = cfg
        self.assertEqual(set(configs), {"alpha", "rick", "rocky", "beta"})
        self.assertIn("home_assistant", configs["alpha"].connections)
        self.assertEqual(set(configs["rick"].roles), {"manager", "maintainer"})

    def test_code_placeholders_expand_to_this_install(self):
        import llm_client
        out = llm_client._expand_server_env({"command": "${OPENCLAVE_PYTHON}", "args": ["${OPENCLAVE_DIR}/mcp_ha_server.py"]})
        self.assertEqual(out["command"], sys.executable)
        self.assertTrue(Path(out["args"][0]).exists())


if __name__ == "__main__":
    unittest.main()
