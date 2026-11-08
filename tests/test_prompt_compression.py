from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import chat_service
import llm_client
import retrieval
import vault
from agents import AGENTS


class PromptCompressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.concept = self.root / "openclave.md"
        self.persona = self.root / "persona.md"

    def test_runtime_section_excludes_reference_without_truncation(self):
        self.concept.write_text(
            "---\nType: Shared context\n---\n"
            "<!-- openclave-runtime:start -->\nKeep domain boundaries.\n"
            "<!-- openclave-runtime:end -->\nLong historical example.\n"
        )
        with patch.object(vault, "concept_file", return_value=self.concept):
            self.assertEqual(retrieval.concept_text(), "Keep domain boundaries.")

    def test_legacy_and_incomplete_markers_keep_all_instructions(self):
        for body in (
            "Original instructions.",
            "<!-- openclave-runtime:start -->\nDo not lose these rules.",
            "<!-- openclave-runtime:start -->\n<!-- openclave-runtime:end -->\nRules.",
        ):
            with self.subTest(body=body):
                self.concept.write_text(body)
                with patch.object(vault, "concept_file", return_value=self.concept):
                    self.assertEqual(retrieval.concept_text(), body)

    def test_routing_is_vault_owned_and_full_persona_is_preserved(self):
        self.persona.write_text('---\nRouting: "Builds and workshop"\n---\nFull persona and feedback.\n')
        with patch.object(retrieval, "AGENTS", {"rocky": AGENTS["rocky"]}), \
             patch.object(vault, "persona_file", return_value=self.persona):
            self.assertEqual(retrieval.agent_roster_text("rocky"), "- Rocky (you): Builds and workshop")
            self.assertEqual(retrieval.persona_text("rocky"), "Full persona and feedback.")
            self.persona.write_text('---\nRouting: "Updated domain"\n---\nFull persona and feedback.\n')
            self.assertIn("Updated domain", retrieval.agent_roster_text("rick"))

    def test_missing_routing_falls_back_to_full_domains(self):
        self.persona.write_text("---\nAgent: Rocky\n---\nPersona.")
        with patch.object(retrieval, "AGENTS", {"rocky": AGENTS["rocky"]}), \
             patch.object(vault, "persona_file", return_value=self.persona), \
             patch.object(vault, "agent_domains", return_value=["Motors", "Workshop"]):
            self.assertEqual(retrieval.agent_roster_text("rick"), "- Rocky: Motors, Workshop")

    def test_prompt_keeps_core_and_summary_without_loading_deferred_context(self):
        fields = ["concept_text", "persona_text", "agent_roster_text"]
        for name in fields:
            patcher = patch.object(retrieval, name, return_value=f"UNIQUE_{name}")
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("mission_text", "plan_summary", "backlog_summary", "foundations_text"):
            patcher = patch.object(retrieval, name, side_effect=AssertionError(f"eager load: {name}"))
            patcher.start()
            self.addCleanup(patcher.stop)
        prompt = chat_service._system_prompt("rocky", "Earlier decisions.")
        for name in fields:
            self.assertEqual(prompt.count(f"UNIQUE_{name}"), 1)
        self.assertIn("Earlier decisions.", prompt)
        self.assertIn("background, not the user's message", prompt)
        for agent_id in AGENTS:
            prompt = chat_service._system_prompt(agent_id, None)
            for get_path in (vault.mission_file, vault.plan_file, vault.backlog_file, vault.foundations_file):
                self.assertIn(str(get_path(agent_id)), prompt)
        self.assertIn("get_foundations(topic)", prompt)

    def test_directory_files_are_read_fresh_in_full_without_search_index(self):
        with patch.object(vault, "VAULT_ROOT", self.root), \
             patch.object(vault, "AGENTS_ROOT", self.root / "Agents"), \
             patch.object(vault.subprocess, "run", side_effect=AssertionError("unexpected QMD read")):
            for get_path in (vault.mission_file, vault.plan_file, vault.backlog_file, vault.foundations_file):
                path = get_path("rocky")
                path.parent.mkdir(parents=True, exist_ok=True)
                body = "Long context.\n" * 600 + "Essential final constraint."
                path.write_text(body)
                self.assertIn(str(path), retrieval.context_directory("rocky"))
                self.assertEqual(llm_client._run_tool("rocky", "read_vault_file", {"path": str(path)}), body)
                path.write_text("Updated context.")
                self.assertEqual(vault.qmd_get(str(path)), "Updated context.")

    def test_absolute_reads_reject_paths_and_symlinks_outside_vault(self):
        allowed = self.root / "vault"
        allowed.mkdir()
        outside = self.root / "private.md"
        outside.write_text("PRIVATE_SENTINEL")
        link = allowed / "link.md"
        link.symlink_to(outside)
        with patch.object(vault, "VAULT_ROOT", allowed):
            for path in (outside, link, allowed / "missing.md"):
                result = vault.qmd_get(str(path))
                self.assertIn("could not read", result)
                self.assertNotIn("PRIVATE_SENTINEL", result)

    def test_qmd_uris_still_use_search_index(self):
        # The real qmd needn't be installed (CI): only the call is checked.
        with patch.object(vault, "qmd_available", return_value=True), patch.object(vault.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "Indexed note."
            self.assertEqual(vault.qmd_get("qmd://vault/note.md"), "Indexed note.")
            self.assertIn("qmd://vault/note.md", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
