import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_factory
import agent_tools
import agents
import chat_service
import llm_client
import llm_models
import vault


class AgentToolsTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        patcher = patch.object(agent_tools, "SELECTION_FILE", tmp / "agent_tools.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_selection_means_everything_except_maintainer_only(self):
        self.assertIn("log_measurement", agent_tools.allowed("spice"))
        self.assertNotIn("update_maintainer_inbox_item", agent_tools.allowed("spice"))
        self.assertIn("update_maintainer_inbox_item", agent_tools.allowed("rick"))

    def test_selection_trims_optional_but_never_core(self):
        agent_tools.set_optional("spice", ["write_note"])
        allowed = agent_tools.allowed("spice")
        self.assertIn("write_note", allowed)
        self.assertNotIn("log_measurement", allowed)
        self.assertTrue(set(agent_tools.CORE_TOOLS) <= allowed)
        with self.assertRaises(ValueError):
            agent_tools.set_optional("spice", ["update_maintainer_inbox_item"])

    def test_dispatcher_refuses_tools_the_agent_does_not_have(self):
        agent_tools.set_optional("spice", [])
        self.assertIn("refused", llm_client._run_tool("spice", "log_measurement", {"metric": "x"}))
        names = {t["function"]["name"] for t in llm_client.tools_for("spice")}
        self.assertNotIn("log_measurement", names)
        self.assertNotIn("finish_onboarding", names)


class AgentFactoryTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        root = tmp / "Agents"
        root.mkdir()
        for patcher in (
            patch.object(agents, "AGENTS_ROOT", root),
            patch.object(vault, "AGENTS_ROOT", root),
            patch.object(agent_tools, "SELECTION_FILE", tmp / "agent_tools.json"),
            patch.object(llm_models, "SELECTION_FILE", tmp / "agent_models.json"),
            patch.dict(agents.AGENTS),
            patch.object(agent_factory, "AVATAR_DIR", tmp / "avatars"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = root

    def test_create_onboard_finish(self):
        created = agent_factory.create_agent("Chef", "Weekly meal planning and grocery lists", "gpt-5.6-terra")
        self.assertEqual(created["id"], "chef")
        folder = self.root / "Chef"
        for name in ("mission.md", "persona.md", "backlog.md", "plan.md", "dashboard.html", "agent.json"):
            self.assertTrue((folder / name).exists(), name)
        self.assertNotIn("{{", (folder / "dashboard.html").read_text())
        self.assertEqual(vault.agent_domains("chef"), ["Weekly meal planning and grocery lists"])
        self.assertEqual(llm_models.selected_model_id("chef"), "gpt-5.6-terra")

        self.assertTrue(agents.is_onboarding("chef"))
        self.assertIn("finish_onboarding", agent_tools.allowed("chef"))
        self.assertNotIn("write_note", agent_tools.allowed("chef"))
        self.assertIn("# Onboarding", chat_service._system_prompt("chef", None))
        greeting = vault.get_conversation("chef", created["conversation_id"])["turns"]
        self.assertEqual(greeting[0]["role"], "assistant")

        result = llm_client._run_tool("chef", "finish_onboarding",
                                      {"tools": ["write_note", "search_vault", "bogus"], "tagline": "Meal plans"})
        self.assertIn("Onboarding finished", result)
        self.assertFalse(agents.is_onboarding("chef"))
        self.assertIn("write_note", agent_tools.allowed("chef"))
        self.assertNotIn("finish_onboarding", agent_tools.allowed("chef"))
        self.assertEqual(agents.AGENTS["chef"].tagline, "Meal plans")
        self.assertIn("Status: active", (folder / "mission.md").read_text())
        self.assertNotIn("# Onboarding", chat_service._system_prompt("chef", None))
        self.assertIn("refused", llm_client._run_tool("chef", "finish_onboarding", {"tools": []}))

    def test_finish_saves_sanitized_avatar_and_survives_a_bad_one(self):
        agent_factory.create_agent("Chef", "Meal planning", "gpt-5.6-terra")
        result = llm_client._run_tool("chef", "finish_onboarding", {
            "tools": [], "avatar_svg": '<svg onload="x()"><circle r="30" fill="#3a6"/><script>x()</script></svg>'})
        self.assertIn("Avatar saved", result)
        saved = (agent_factory.AVATAR_DIR / "chef.svg").read_text()
        self.assertIn("<circle", saved)
        self.assertNotIn("script", saved)
        self.assertNotIn("onload", saved)

        agent_factory.create_agent("Baker", "Bread", "gpt-5.6-terra")
        result = llm_client._run_tool("baker", "finish_onboarding", {"tools": [], "avatar_svg": "<html/>"})
        self.assertIn("Onboarding finished", result)
        self.assertIn("Avatar not saved", result)
        self.assertFalse(agents.is_onboarding("baker"))

    def test_rejects_taken_reserved_and_invalid_names(self):
        for name in ("Reba", "api", "", "x" * 30, "bad/name"):
            with self.assertRaises(ValueError):
                agent_factory.create_agent(name, "something", "gpt-5.6-terra")
        with self.assertRaises(ValueError):
            agent_factory.create_agent("Chef", "   ", "gpt-5.6-terra")

    def test_created_agent_reloads_from_vault(self):
        agent_factory.create_agent("Chef", "Meal planning", "gpt-5.6-terra")
        del agents.AGENTS["chef"]
        agents._load_created_agents()
        self.assertEqual(agents.AGENTS["chef"].folder, "Chef")
        data = json.loads((self.root / "Chef" / "agent.json").read_text())
        self.assertTrue(data["onboarding"])


if __name__ == "__main__":
    unittest.main()
