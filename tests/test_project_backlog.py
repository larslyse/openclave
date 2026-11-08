"""The configured project backlog is writable only through maintainer backlog tools."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import agents
import vault
from agents import AgentConfig
from config import load_config


class ProjectBacklogTests(unittest.TestCase):
    def test_config_rejects_backlog_outside_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_file = root / "openclave.toml"
            config_file.write_text('[paths]\nvault_root = "vault"\nproject_backlog = "../outside.md"\n')
            with self.assertRaisesRegex(ValueError, "project_backlog"):
                load_config(config_file, environ={"OPENCLAVE_HOME": str(root)})

    def test_maintainer_uses_shared_backlog_and_other_agent_stays_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_root = root / "agents"
            shared = root / "project" / "backlog.md"
            shared.parent.mkdir()
            shared.write_text("# Project\n- [ ] Initial\n")
            (agents_root / "Rick").mkdir(parents=True)
            (agents_root / "Other").mkdir()
            local = agents_root / "Rick" / "backlog.md"
            local.write_text("# Old Rick backlog\n- [ ] Legacy\n")
            registry = {
                "rick": AgentConfig("rick", "Rick", "Rick", "", "", roles=("maintainer",)),
                "other": AgentConfig("other", "Other", "Other", "", ""),
            }
            with patch.dict(agents.AGENTS, registry, clear=True), patch.object(vault, "CONFIG", replace(vault.CONFIG, agents_root=agents_root, project_backlog_file=shared)), patch.object(vault, "AGENTS_ROOT", agents_root):
                self.assertEqual(vault.backlog_file("rick"), shared)
                self.assertEqual(vault.backlog_file("other"), agents_root / "Other" / "backlog.md")
                vault.backlog_add("rick", "Shared task")
                self.assertTrue(vault.backlog_set_done("rick", "Shared task", True))
                vault.backlog_add("other", "Local task")
                self.assertIn("- [x] Shared task", shared.read_text())
                self.assertIn("- [ ] Legacy", local.read_text())
                self.assertNotIn("Shared task", local.read_text())
                self.assertIn("- [ ] Local task", (agents_root / "Other" / "backlog.md").read_text())
                self.assertEqual(vault.read_backlog("rick")["items"][-1], {"text": "Shared task", "done": True})


if __name__ == "__main__":
    unittest.main()
