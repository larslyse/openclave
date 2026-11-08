import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_factory
import agent_tools
import agents
import llm_models
import vault

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "Agents"


class AgentEditTests(unittest.TestCase):
    """Runs against a synthetic built-in agent folder in a temp dir."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        root = tmp / "Agents"
        root.mkdir()
        shutil.copytree(FIXTURE_ROOT / "Spice", root / "Spice")
        (root / "Spice" / "conversations").mkdir()
        for patcher in (
            patch.object(agents, "AGENTS_ROOT", root),
            patch.object(vault, "AGENTS_ROOT", root),
            patch.object(agent_tools, "SELECTION_FILE", tmp / "t.json"),
            patch.object(llm_models, "SELECTION_FILE", tmp / "m.json"),
            patch.dict(agents.AGENTS),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = root
        self.cid = vault.new_conversation_id()
        vault.append_turn("spice", self.cid, "user", "what music do I like?")
        vault.append_turn("spice", self.cid, "assistant", "Mostly 90s trip-hop.")

    def test_rename_keeps_old_transcripts_readable(self):
        agent_factory.rename_agent("spice", "Pepper", "Taste and preferences")
        self.assertEqual(agents.AGENTS["spice"].name, "Pepper")
        turns = vault.get_conversation("spice", self.cid)["turns"]
        self.assertEqual([t["role"] for t in turns], ["user", "assistant"])
        vault.append_turn("spice", self.cid, "assistant", "And some jazz.")
        self.assertEqual(len(vault.get_conversation("spice", self.cid)["turns"]), 3)

        agent_factory.rename_agent("spice", "Salt", "Taste and preferences")
        self.assertEqual(agents.AGENTS["spice"].former_names, ("Spice", "Pepper"))
        self.assertEqual(len(vault.get_conversation("spice", self.cid)["turns"]), 3)

    def test_rename_updates_page_and_survives_reload(self):
        agent_factory.rename_agent("spice", "Pepper", "Taste and preferences")
        page = (self.root / "Spice" / "dashboard.html").read_text()
        self.assertIn("window.OPENCLAVE_AGENT_NAME = 'Pepper'", page)
        self.assertNotIn("'Spice'", page)
        agents.AGENTS.clear()
        agents._load_created_agents()
        self.assertEqual(agents.AGENTS["spice"].name, "Pepper")
        self.assertEqual(agents.AGENTS["spice"].folder, "Spice")
        data = json.loads((self.root / "Spice" / "agent.json").read_text())
        self.assertNotIn("onboarding", data)
        self.assertFalse(agents.is_onboarding("spice"))

    def test_rename_rejects_taken_and_invalid(self):
        for name, tagline in (("Reba", "x"), ("bad/name", "x"), ("Pepper", ""), ("Pepper", "x" * 61)):
            with self.assertRaises(ValueError):
                agent_factory.rename_agent("spice", name, tagline)
        agent_factory.rename_agent("spice", "Spice", "New tagline only")  # same name, new tagline: fine
        self.assertEqual(agents.AGENTS["spice"].former_names, ())

    def test_identity_edit_keeps_frontmatter_and_enforces_limit(self):
        before = vault.read_identity_body("spice", "persona")
        vault.set_identity_body_by_lars("spice", "persona", "Short and quiet.\n\n## Feedback")
        text = vault.persona_file("spice").read_text()
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("Routing:", text)
        self.assertIn("Short and quiet.", text)
        with self.assertRaises(ValueError):
            vault.set_identity_body_by_lars("spice", "persona", "x" * 2300)
        self.assertIn("Short and quiet.", vault.persona_file("spice").read_text())
        self.assertEqual(before["limit"], 2200)

    def test_moving_a_renamed_agents_conversation_keeps_its_turns(self):
        agent_factory.rename_agent("spice", "Pepper", "Taste and preferences")
        shutil.copytree(FIXTURE_ROOT / "Fresh", self.root / "Fresh")
        (self.root / "Fresh" / "conversations").mkdir()
        vault.move_conversation("spice", "fresh", self.cid)
        self.assertEqual(len(vault.get_conversation("fresh", self.cid)["turns"]), 2)


if __name__ == "__main__":
    unittest.main()


class AgentColorTests(unittest.TestCase):
    def test_set_color(self):
        import tempfile, dataclasses
        from pathlib import Path
        from unittest.mock import patch
        import agents, agent_factory
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Body").mkdir()
            (root / "Body" / "agent.json").write_text('{"id": "body", "name": "Body", "color": "#aa0000", "tagline": "t"}')
            (root / "Body" / "dashboard.html").write_text(":root{\n  --accent: #aa0000;\n  --accent-dark: #AA0000;\n  --other: #aa0000;\n}")
            cfg = agents.AgentConfig("body", "Body", "Body", "#aa0000", "t")
            with patch.dict(agents.AGENTS, {"body": cfg}, clear=True), patch.object(agents, "AGENTS_ROOT", root):
                self.assertEqual(agent_factory.set_color("body", "#00FF88"), {"id": "body", "color": "#00FF88"})
                self.assertEqual(agents.AGENTS["body"].color, "#00FF88")
                self.assertIn('"color": "#00FF88"', (root / "Body" / "agent.json").read_text())
                page = (root / "Body" / "dashboard.html").read_text()
                self.assertIn("--accent: #00FF88;", page)
                self.assertIn("--accent-dark: #00FF88;", page)
                self.assertIn("--other: #aa0000;", page, "only the agent-colour variables")
                with self.assertRaises(ValueError):
                    agent_factory.set_color("body", "red; }")

    def test_tinted_avatar_follows_colour(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from PIL import Image
        import agents, agent_factory
        with tempfile.TemporaryDirectory() as tmp:
            root, avatars = Path(tmp) / "agents", Path(tmp) / "avatars"
            (root / "Body").mkdir(parents=True)
            avatars.mkdir()
            (root / "Body" / "agent.json").write_text('{"id": "body", "name": "Body", "color": "#aa0000"}')
            Image.new("RGBA", (32, 32), (128, 128, 128, 255)).save(avatars / "body.base.png")
            cfg = agents.AgentConfig("body", "Body", "Body", "#aa0000", "t")
            with patch.dict(agents.AGENTS, {"body": cfg}, clear=True), patch.object(agents, "AGENTS_ROOT", root), \
                    patch.object(agent_factory, "AVATAR_DIR", avatars):
                agent_factory.set_color("body", "#0000ff")
                r, g, b, _ = Image.open(avatars / "body.png").getpixel((16, 16))
                self.assertGreater(b, r)
                agent_factory.save_svg_avatar("body", '<svg xmlns="http://www.w3.org/2000/svg"/>')
                self.assertFalse((avatars / "body.base.png").exists(), "a new avatar drops the tint source")
