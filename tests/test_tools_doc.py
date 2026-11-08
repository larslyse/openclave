import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import llm_client
import tools_doc

TOOL = {"type": "function", "function": {
    "name": "search_vault", "description": "Search the vault.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "a | b"}},
                   "required": ["query"]},
}}


class ToolsDocTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "tools overview.md"
        for patcher in (patch.object(tools_doc, "DOC_PATH", self.path),
                        patch.object(llm_client, "TOOLS", [TOOL])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_writes_full_description_and_escapes_table_pipes(self):
        self.assertTrue(tools_doc.write_if_changed())
        text = self.path.read_text()
        self.assertIn("### search_vault", text)
        self.assertIn("> Search the vault.", text)
        self.assertIn("a \\| b", text)
        self.assertNotIn(tools_doc.UPDATED_PLACEHOLDER, text)

    def test_unchanged_tools_do_not_rewrite_even_on_a_later_day(self):
        tools_doc.write_if_changed()
        text = self.path.read_text().replace("Updated: ", "Updated: 2000-01-01 ", 1)
        stale_date = text.replace("Updated: 2000-01-01 " + text.split("Updated: 2000-01-01 ")[1].split("\n")[0],
                                  "Updated: 2000-01-01")
        self.path.write_text(stale_date)
        self.assertFalse(tools_doc.write_if_changed())
        self.assertIn("Updated: 2000-01-01", self.path.read_text())

    def test_changed_tool_rewrites(self):
        tools_doc.write_if_changed()
        changed = {"type": "function", "function": {**TOOL["function"], "description": "New text."}}
        with patch.object(llm_client, "TOOLS", [changed]):
            self.assertTrue(tools_doc.write_if_changed())
        self.assertIn("> New text.", self.path.read_text())


if __name__ == "__main__":
    unittest.main()
