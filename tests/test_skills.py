import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agents
import llm_client
import skills
import vault
from config import load_config

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "Agents"

KNOW_HOW = """---
name: ha-update
description: Update Home Assistant safely and check release notes for breaking changes.
---

1. Read the release notes for every version being skipped.
2. Take a backup first.
"""
OPENCLAW_SKILL = """---
name: qmd
description: Semantic search over the vault with qmd.
metadata: {"openclaw": {"always": true, "requires": {"bins": ["qmd"], "env": ["QMD_INDEX"]}}}
---

Run this:

```bash
qmd --index vault-cli query "term"
```
"""


class SkillsTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        root = tmp / "Agents"
        root.mkdir()
        for name in ("Spice", "R2"):
            shutil.copytree(FIXTURE_ROOT / name, root / name)
        self.shared = tmp / "shared-skills"
        self.src = tmp / "outside"
        for patcher in (
            patch.object(agents, "AGENTS_ROOT", root),
            patch.object(vault, "AGENTS_ROOT", root),
            patch.object(skills, "SELECTION_FILE", tmp / "agent_skills.json"),
            patch.object(skills, "shared_root", lambda: self.shared),
            patch.dict(agents.AGENTS, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        # Only the fixture agents, whatever the local install has.
        agents._load_created_agents()
        self.agent_dir = root / "R2"

    def write_source(self, text: str, extra: dict | None = None) -> Path:
        folder = self.src / skills.slug(skills.parse(text)[0].get("name", "x"))
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(text, encoding="utf-8")
        for name, content in (extra or {}).items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(content, encoding="utf-8")
        return folder

    def test_agent_saves_and_reads_its_own_skill(self):
        saved = skills.save_own("r2", "HA Update", "Update HA safely.", "1. Backup\n2. Update")
        self.assertEqual(saved["name"], "ha-update")
        self.assertTrue((self.agent_dir / "skills" / "ha-update" / "SKILL.md").is_file())
        listed = skills.list_for("r2")
        self.assertEqual([(s["key"], s["enabled"]) for s in listed], [("own/ha-update", True)])
        self.assertIn("- ha-update: Update HA safely.", skills.prompt_line("r2"))
        self.assertEqual(skills.prompt_line("spice"), "", "own skills are only for that agent")
        self.assertIn("1. Backup", skills.read("r2", "ha-update"))
        for bad in (("", "d", "b"), ("x", "", "b"), ("x", "d", ""), ("x", "d" * 301, "b")):
            with self.assertRaises(skills.SkillError):
                skills.save_own("r2", *bad)

    def test_tools_dispatch_and_core(self):
        import agent_tools
        self.assertIn("read_skill", agent_tools.CORE_TOOLS)
        self.assertIn("read_skill", llm_client.READ_ONLY_TOOLS)
        out = llm_client._run_tool("r2", "save_skill", {"name": "net", "description": "Network checks.", "instructions": "Ping."})
        self.assertIn("skill saved: net", out)
        self.assertIn("Ping.", llm_client._run_tool("r2", "read_skill", {"name": "net"}))
        self.assertTrue(llm_client._run_tool("r2", "read_skill", {"name": "nope"}).startswith("refused:"))

    def test_shared_skill_switches_per_agent(self):
        (self.shared / "tone").mkdir(parents=True)
        (self.shared / "tone" / "SKILL.md").write_text("---\nname: tone\ndescription: How to write.\n---\nShort.\n")
        self.assertTrue(all(s["enabled"] for s in skills.list_for("spice") + skills.list_for("r2")))
        skills.set_enabled("spice", "shared/tone", False)
        self.assertFalse(skills.list_for("spice")[0]["enabled"])
        self.assertTrue(skills.list_for("r2")[0]["enabled"])
        with self.assertRaises(skills.SkillError):
            skills.read("spice", "tone")

    def test_plain_know_how_imports_with_no_warnings_but_switched_off(self):
        source = self.write_source(KNOW_HOW)
        preview = skills.import_skill(str(source), target="r2", preview=True)
        self.assertEqual((preview["name"], preview["warnings"]), ("ha-update", []))
        self.assertFalse((self.agent_dir / "skills" / "ha-update").exists(), "preview writes nothing")
        skills.import_skill(str(source), target="r2")
        skill = skills.list_for("r2")[0]
        self.assertEqual((skill["approved"], skill["enabled"]), (False, False))
        self.assertEqual(skills.prompt_line("r2"), "")
        with self.assertRaises(skills.SkillError):
            skills.set_enabled("r2", skill["key"], True)
        skills.approve("r2", skill["key"])
        self.assertTrue(skills.list_for("r2")[0]["enabled"])
        self.assertIn("ha-update", skills.prompt_line("r2"))

    def test_openclaw_skill_is_flagged_for_what_it_expects(self):
        source = self.write_source(OPENCLAW_SKILL, {"scripts/search.sh": "qmd query $1\n", "logo.png": "x"})
        report = skills.import_skill(str(source), target="shared")
        warnings = " | ".join(report["warnings"])
        self.assertIn("scripts agents can't run (scripts/search.sh)", warnings)
        self.assertIn("shell commands", warnings)
        self.assertIn("programs installed (qmd)", warnings)
        self.assertIn("settings or keys (QMD_INDEX)", warnings)
        self.assertEqual(report["files"], ["scripts/search.sh"], "binary files aren't copied")
        skills.approve("spice", "shared/qmd")
        text = skills.read("spice", "qmd")
        self.assertIn("Imported from another harness", text)
        self.assertIn("scripts/search.sh", text)
        self.assertIn("qmd query", skills.read("spice", "qmd", "scripts/search.sh"))
        with self.assertRaises(skills.SkillError):
            skills.read("spice", "qmd", "../../R2/agent.json")

    def test_import_refuses_duplicates_and_bad_input(self):
        source = self.write_source(KNOW_HOW)
        skills.import_skill(str(source), target="shared")
        with self.assertRaises(skills.SkillError):
            skills.import_skill(str(source), target="shared")
        skills.import_skill(str(source), target="shared", replace=True)
        with self.assertRaises(skills.SkillError):
            skills.import_skill("/no/such/folder")
        with self.assertRaises(skills.SkillError):
            skills.import_skill(pasted="---\nname: empty\n---\n")
        with self.assertRaises(skills.SkillError):
            skills.import_skill(str(source), target="nobody")
        self.assertEqual(skills.import_skill(pasted=KNOW_HOW, target="spice", name="mine")["name"], "mine")

    def test_agent_cant_overwrite_an_imported_skill(self):
        skills.import_skill(str(self.write_source(KNOW_HOW)), target="r2")
        with self.assertRaises(skills.SkillError):
            skills.save_own("r2", "ha-update", "Mine now.", "x")

    def test_github_links(self):
        self.assertEqual(skills._github_parts("https://github.com/o/r/tree/main/skills/qmd"),
                         ("o", "r", "main", "skills/qmd", "tree"))
        self.assertIsNone(skills._github_parts("https://example.com/o/r/tree/main/x"))
        fetched = []

        def fake_fetch(url, limit=skills.MAX_IMPORT_BYTES):
            fetched.append(url)
            if "api.github.com" in url:
                return json.dumps([{"type": "file", "name": "SKILL.md", "size": 10, "download_url": "https://raw.test/SKILL.md"},
                                   {"type": "file", "name": "pic.png", "size": 10, "download_url": "https://raw.test/pic.png"}]).encode()
            return KNOW_HOW.encode()
        with patch.object(skills, "_fetch", side_effect=fake_fetch):
            report = skills.import_skill("https://github.com/o/r/tree/main/skills/ha-update", preview=True)
        self.assertEqual(report["name"], "ha-update")
        self.assertNotIn("https://raw.test/pic.png", fetched)

    def test_config_default_and_example(self):
        # The built-in default, not this machine's openclave.toml.
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(load_config(environ={"OPENCLAVE_HOME": home}).skills_root.name, "skills")


if __name__ == "__main__":
    unittest.main()
