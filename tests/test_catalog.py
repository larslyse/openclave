import dataclasses
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import agent_factory
import agent_tools
import agents
import catalog
import chat_service
import llm_client
import llm_models
import svg_avatar
import vault
import config
from config import CONFIG

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import export_agent  # noqa: E402
import add_manager  # noqa: E402,F401

MODEL = "gpt-5.6-terra"


class CatalogContentTests(unittest.TestCase):
    """The shipped templates are valid, generic and within the limits."""

    def test_every_builtin_template_is_complete(self):
        templates = {t["id"]: t for t in catalog.templates()}
        self.assertGreaterEqual(len(templates), 10)
        for t in templates.values():
            folder = t["folder"]
            agents._agent_config(t["data"], folder.name)  # loads like a real agent.json
            self.assertTrue(t["summary"] and t["category"], t["id"])
            self.assertLessEqual(len((folder / "mission.md").read_text()), 1850, t["id"])
            self.assertLessEqual(len((folder / "persona.md").read_text()), 2200, t["id"])
            self.assertTrue(catalog._questions(folder), f"{t['id']} has onboarding questions")
            self.assertIn("## Feedback", (folder / "persona.md").read_text(), t["id"])
            for tool in t["suggested_tools"] or []:
                self.assertIn(tool, agent_tools.OPTIONAL_TOOLS, (t["id"], tool))

    def test_templates_talk_about_the_user_not_one_owner(self):
        for t in catalog.templates():
            text = " ".join((t["folder"] / f).read_text() for f in ("mission.md", "persona.md", "onboarding.md"))
            self.assertNotRegex(text, r"\bhis user\b", t["id"])


class CatalogInstallTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        root = tmp / "Agents"
        root.mkdir()
        for patcher in (
            patch.object(agents, "AGENTS_ROOT", root),
            patch.object(vault, "AGENTS_ROOT", root),
            patch.object(agent_tools, "SELECTION_FILE", tmp / "agent_tools.json"),
            patch.object(llm_models, "SELECTION_FILE", tmp / "agent_models.json"),
            patch.dict(agents.AGENTS, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root, self.tmp = root, tmp

    def test_install_starts_onboarding_with_the_template(self):
        created = catalog.install("bolt", MODEL)
        folder = self.root / "Bolt"
        data = json.loads((folder / "agent.json").read_text())
        self.assertTrue(data["onboarding"])
        self.assertEqual(data["template"]["id"], "bolt")
        self.assertIn("home_assistant", agents.AGENTS["bolt"].connections, "connections carried over")
        self.assertTrue((folder / "avatar.svg").is_file())
        self.assertIn("Home Assistant", (folder / "mission.md").read_text())
        self.assertIn("Status: onboarding", (folder / "mission.md").read_text())
        self.assertEqual(set(agent_tools.selected_optional("bolt")),
                         set(catalog.get("bolt")["suggested_tools"]))
        opening = vault.get_conversation("bolt", created["conversation_id"])["turns"][0]["content"]
        self.assertIn("ready-made", opening)
        self.assertIn(data["template"]["questions"][0], opening)
        prompt = chat_service._system_prompt("bolt", None)
        self.assertIn("installed from a ready-made template", prompt)
        self.assertIn(data["template"]["questions"][1], prompt)
        self.assertIn("finish_onboarding", agent_tools.allowed("bolt"))
        self.assertIn("Should I create these topics for you?", prompt, "onboarding offers topics from the domains")
        done = llm_client._run_tool("bolt", "finish_onboarding", {"tools": ["write_note"], "topics": ["Wifi", "Home Assistant", "wifi", ""]})
        self.assertIn("Onboarding finished", done)
        self.assertIn("Topics: Wifi, Home Assistant.", done)
        import folders
        self.assertEqual([f["name"] for f in folders.list_folders("bolt")], ["Wifi", "Home Assistant"], "duplicates and blanks skipped")
        self.assertNotIn("installed from a ready-made template", chat_service._system_prompt("bolt", None))
        self.assertEqual(catalog.installed_from(), {"bolt": ["bolt"]})

    def test_install_under_another_name(self):
        catalog.install("bolt", MODEL)
        with self.assertRaisesRegex(ValueError, "taken"):
            catalog.install("bolt", MODEL)
        catalog.install("bolt", MODEL, name="Sparky")
        self.assertIn("sparky", agents.AGENTS)

    def test_the_manager_has_no_default_name_or_persona(self):
        t = catalog.get("rick")
        self.assertTrue(t["manager"])
        self.assertEqual([p["id"] for p in t["personas"]], ["calm", "warm", "blunt"])
        self.assertTrue(t["job"])
        for name in (None, "", "  "):
            with self.assertRaisesRegex(ValueError, "name"):
                catalog.install("rick", MODEL, name=name, persona="calm")
        with self.assertRaisesRegex(ValueError, "personality"):
            catalog.install("rick", MODEL, name="Morty")
        with self.assertRaisesRegex(ValueError, "few more words"):
            catalog.install("rick", MODEL, name="Morty", persona="nice")
        catalog.install("rick", MODEL, name="Morty", persona="calm", mission_extra="Skip the vault suggestions.\nAlso watch my subscriptions.")
        persona = (self.root / "Morty" / "persona.md").read_text()
        self.assertIn("Morty keeps OpenClave itself working: a calm", persona)
        self.assertNotIn("Rick", persona)
        self.assertNotIn("Label:", persona)
        mission = (self.root / "Morty" / "mission.md").read_text()
        self.assertIn("## The user's changes", mission)
        self.assertIn("> Also watch my subscriptions.", mission)
        questions = json.loads((self.root / "Morty" / "agent.json").read_text())["template"]["questions"]
        self.assertFalse([q for q in questions if "Rick" in q])
        self.assertEqual(set(agents.AGENTS["morty"].roles), {"manager", "maintainer"})
        self.assertEqual(agents.AGENTS["morty"].cli_tools, ("WebSearch", "WebFetch"))

    def test_manager_in_the_users_own_words(self):
        catalog.install("rick", MODEL, name="Sage", persona="Dry humour, very patient, never uses jargon.")
        persona = (self.root / "Sage" / "persona.md").read_text()
        self.assertIn("> Dry humour, very patient, never uses jargon.", persona)
        self.assertIn("update_persona", persona)
        self.assertNotIn("The user's changes", (self.root / "Sage" / "mission.md").read_text())
        public = [x for x in catalog.listing() if x["id"] == "rick"][0]
        json.dumps(public)  # no file paths leak into the API
        self.assertEqual(set(public["personas"][0]), {"id", "label", "summary"})

    def test_add_manager_script(self):
        import add_manager
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(add_manager.main(["--status"]), 4)
            self.assertEqual(add_manager.main(["--check", "Bad/name"]), 2)
            self.assertEqual(add_manager.main(["--check", "Zed"]), 0)
            self.assertNotIn("zed", agents.AGENTS)
            self.assertEqual(add_manager.main(["Zed"]), 2, "needs a personality")
            self.assertEqual(add_manager.main(["Zed", "--persona", "warm", "--mission-extra", "Also my budget."]), 0)
            self.assertEqual(add_manager.main(["Other", "--persona", "calm"]), 3, "only one manager")
            self.assertEqual(add_manager.main(["--status"]), 0)
        self.assertEqual(out.getvalue().split()[-1], "Zed")
        self.assertIn("manager", agents.AGENTS["zed"].roles)

    def test_manager_starts_blank_with_suggestions(self):
        t = catalog.get("rick")
        self.assertFalse(t["has_avatar"], "initials until the user picks a picture")
        self.assertTrue(t["name_ideas"])
        self.assertNotIn("Rick", t["name_ideas"])
        self.assertEqual([x["id"] for x in t["extras"]], ["business-ideas", "agent-scout"])
        with self.assertRaisesRegex(ValueError, "extra job"):
            catalog.install("rick", MODEL, name="Ada", persona="calm", extras=["nope"])
        catalog.install("rick", MODEL, name="Ada", persona="calm")
        mission = (self.root / "Ada" / "mission.md").read_text()
        self.assertNotIn("usiness ideas", mission, "extra jobs only when picked")
        self.assertFalse((self.root / "Ada" / "avatar.svg").exists())

    def test_picked_extra_jobs_join_the_mission(self):
        catalog.install("rick", MODEL, name="Ada", persona="warm", extras=["business-ideas", "business-ideas"])
        mission = (self.root / "Ada" / "mission.md").read_text()
        self.assertEqual(mission.count("## Business ideas"), 1)
        self.assertNotIn("Label:", mission)

    def test_the_longest_manager_setup_fits_the_self_edit_limits(self):
        t = catalog.get("rick")
        with self.assertRaisesRegex(ValueError, "300"):
            catalog.install("rick", MODEL, name="Other", persona="calm", mission_extra="y" * 301)
        catalog.install("rick", MODEL, name="Maximilian-Alexander-Lon", persona="x" * catalog.PERSONA_MAX,
                        extras=[x["id"] for x in t["extras"]], mission_extra="y" * catalog.MISSION_EXTRA_MAX)
        for kind in ("mission", "persona"):
            size = len((self.root / "Maximilian-Alexander-Lon" / f"{kind}.md").read_text())
            self.assertLessEqual(size, vault.IDENTITY_CHAR_LIMITS[kind], kind)

    def test_avatar_library(self):
        library = catalog.avatar_library()
        ids = [a["id"] for a in library]
        self.assertIn("keeper", ids)
        self.assertIn("scientist", ids)
        for a in library:
            svg_avatar.sanitize(catalog.library_avatar(a["id"]))  # every picture survives the sanitizer
            self.assertTrue(a["label"])
        with self.assertRaises(ValueError):
            catalog.library_avatar("../agents/rick/persona")

    def test_add_agents_script(self):
        import add_agents
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(add_agents.main(["--list"]), 0)
            listed = [line.split("\t")[0] for line in out.getvalue().splitlines()]
            self.assertNotIn("rick", listed, "the manager is its own step")
            self.assertIn("spice", listed)
            self.assertEqual(add_agents.main(["spice", "nope"]), 2)
        self.assertIn("spice", agents.AGENTS)
        self.assertIn("Couldn't add nope", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            add_agents.main(["--list"])
        self.assertNotIn("spice\t", out.getvalue(), "added ones drop off the list")

    def test_null_suggestions_mean_every_optional_tool(self):
        catalog.install("spice", MODEL)
        self.assertEqual(set(agent_tools.selected_optional("spice")), set(agent_tools.optional_for("spice")))

    def test_extra_source_and_unknown_ids(self):
        extra = self.tmp / "community"
        shutil.copytree(catalog.BUILTIN / "greg", extra / "agents" / "fern")
        data = json.loads((extra / "agents" / "fern" / "agent.json").read_text())
        (extra / "agents" / "fern" / "agent.json").write_text(json.dumps({**data, "id": "fern", "name": "Fern"}))
        (extra / "agents" / "broken").mkdir()
        with patch.object(catalog, "CONFIG", dataclasses.replace(CONFIG, catalog_sources=(extra,))):
            ids = {t["id"]: t["source"] for t in catalog.templates()}
        self.assertEqual(ids["fern"], str(extra))
        self.assertEqual(ids["greg"], "built-in")
        self.assertNotIn("broken", ids)
        with self.assertRaises(ValueError):
            catalog.install("nope", MODEL)


class ExportTests(unittest.TestCase):
    def test_export_drafts_a_template_and_lists_personal_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Alex"
            src.mkdir()
            (src / "agent.json").write_text(json.dumps({"id": "alex", "name": "Alex", "color": "#123456", "tagline": "Help"}))
            (src / "mission.md").write_text("---\nAgent: Alex\n---\nHelp Sam with my house from 1967.\n")
            (src / "persona.md").write_text("Calm.\n\n## Feedback\n")
            (src / "conversations").mkdir()
            (src / "conversations" / "x.md").write_text("private chat")
            dest = Path(tmp) / "out" / "alex"
            out = io.StringIO()
            with redirect_stdout(out), patch.object(config, "CONFIG", dataclasses.replace(CONFIG, owner_name="Sam")), \
                    patch.dict("os.environ", {"OPENCLAVE_PRIVATE_STRINGS": str(Path(tmp) / "none.txt")}):
                export_agent.main([str(src), str(dest)])
            self.assertEqual((dest / "mission.md").read_text(), "Help Sam with my house from 1967.\n")
            self.assertFalse((dest / "conversations").exists())
            self.assertIn("owner name 'Sam'", out.getvalue())
            self.assertIn("catalog", json.loads((dest / "agent.json").read_text()))


if __name__ == "__main__":
    unittest.main()
