"""make_pdf (pdf_export.py): which note a request means, and Work/ never exported."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_tools  # noqa: E402
import llm_client  # noqa: E402
import pdf_export  # noqa: E402
import vault  # noqa: E402


class ResolveNote(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        for rel in ("Personal/Resources/Heat pump/Warranty claim heat pump.md",
                    "Personal/A/backlog.md", "Personal/B/backlog.md", "Work/Secret plan.md", ".trash/Old.md"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("# x\n", encoding="utf-8")
        self.root = root
        self.patches = [patch.object(vault, "VAULT_ROOT", root), patch.object(pdf_export, "CONFIDENTIAL", root / "Work")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def rel(self, note):
        return str(pdf_export.resolve_note(note).relative_to(self.root))

    def test_name_path_and_qmd_uri_find_the_same_note(self):
        want = "Personal/Resources/Heat pump/Warranty claim heat pump.md"
        self.assertEqual(self.rel("Warranty claim heat pump"), want)
        self.assertEqual(self.rel(want), want)
        self.assertEqual(self.rel(str(self.root / want)), want)
        self.assertEqual(self.rel("qmd://notes/personal/resources/heat-pump/warranty-claim-heat-pump.md"), want)

    def test_several_matches_lists_the_paths(self):
        with self.assertRaisesRegex(pdf_export.PdfError, "Personal/A/backlog.md"):
            pdf_export.resolve_note("backlog")

    def test_confidential_and_trash_are_never_exported(self):
        with self.assertRaisesRegex(pdf_export.PdfError, "Work"):
            pdf_export.resolve_note("Work/Secret plan.md")
        with self.assertRaisesRegex(pdf_export.PdfError, "Work"):
            pdf_export.resolve_note(str(self.root / "Work/Secret plan.md"))
        for name in ("Secret plan", "Old"):
            with self.assertRaisesRegex(pdf_export.PdfError, "no note found"):
                pdf_export.resolve_note(name)

    def test_outside_the_vault_is_refused(self):
        with self.assertRaises(pdf_export.PdfError):
            pdf_export.resolve_note("/etc/hostname")


class ToolWiring(unittest.TestCase):
    def test_make_pdf_is_an_optional_tool_with_a_schema(self):
        self.assertIn("make_pdf", agent_tools.OPTIONAL_TOOLS)
        self.assertIn("make_pdf", {t["function"]["name"] for t in llm_client.TOOLS})
        self.assertEqual(llm_client.tool_phase("make_pdf"), "making a pdf")

    def test_errors_come_back_as_text(self):
        with patch.object(agent_tools, "allowed", lambda a: {"make_pdf"}), \
             patch.object(pdf_export, "make_pdf", side_effect=pdf_export.PdfError("no note found for 'x'")), \
             patch.object(llm_client.request_phase, "set_phase", lambda *a: None):
            self.assertEqual(llm_client._run_tool("rick", "make_pdf", {"note": "x"}), "no PDF: no note found for 'x'")


if __name__ == "__main__":
    unittest.main()
