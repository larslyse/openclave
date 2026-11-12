"""Scoped vault write access (vault_access.py). Temp vault only."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import llm_client
import vault
import vault_access


class VaultAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.root = root
        for p in (patch.object(vault, "VAULT_ROOT", root), patch.object(vault, "AGENTS_ROOT", root / "OpenClave" / "Agents")):
            p.start()
            self.addCleanup(p.stop)
        vault.agent_root("rick").mkdir(parents=True)
        (vault.agent_root("rick") / "agent.json").write_text(json.dumps({"id": "rick"}))
        (root / "Area" / "Blog").mkdir(parents=True)
        (root / "Area" / "Other").mkdir(parents=True)
        (root / "Area" / "Blog" / "Ideas.md").write_text("# Ideas\n\n- one\n")

    def test_no_folders_means_no_writes(self):
        self.assertIn("no vault folders", vault_access.write("rick", "Area/Blog/Ideas.md", "- two", "append", "x"))
        self.assertEqual(vault_access.prompt_line("rick"), "")

    def test_writes_only_inside_the_given_folders(self):
        self.assertEqual(vault_access.set_folders("rick", ["Area/Blog/", "Area/Blog"]), ["Area/Blog"])
        self.assertTrue(vault_access.write("rick", "Area/Blog/Ideas.md", "- two", "append", "add idea").startswith("Success!"))
        self.assertIn("- two", (self.root / "Area/Blog/Ideas.md").read_text())
        self.assertTrue(vault_access.write("rick", "Area/Blog/New post.md", "# Post", "create", "new").startswith("Success!"))
        self.assertIn("outside your vault folders", vault_access.write("rick", "Area/Other/x.md", "y", "create", "n"))
        self.assertIn("refused", vault_access.write("rick", "Area/Blog/../Other/x.md", "y", "create", "n"))
        self.assertIn("Area/Blog", vault_access.prompt_line("rick"))

    def test_folders_must_exist_and_not_be_agent_folders(self):
        with self.assertRaises(ValueError):
            vault_access.set_folders("rick", ["Nope"])
        with self.assertRaises(ValueError):
            vault_access.set_folders("rick", ["OpenClave/Agents"])
        vault_access.set_folders("rick", ["Area/Blog"])
        self.assertEqual(vault_access.set_folders("rick", []), [])
        self.assertNotIn("vault_write", json.loads((vault.agent_root("rick") / "agent.json").read_text()))

    def test_tool_is_registered(self):
        self.assertIn("write_vault_note", {t["function"]["name"] for t in llm_client.TOOLS})
        self.assertEqual(llm_client.tool_phase("write_vault_note"), "writing files")


if __name__ == "__main__":
    unittest.main()
