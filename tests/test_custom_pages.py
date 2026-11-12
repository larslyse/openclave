"""An agent's own phone page (custom_pages.py): page.json blocks over the
agent's own files, checked before the owner sees them. Runs on synthetic fixtures."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agents
import custom_pages
import llm_client
import vault

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "Agents"
CATALOG = """# Stash — Stock Catalog

Status values: `on shelf`, `claimed`

## Electronics components

| Item | Qty | Location | Status | Notes |
|---|---|---|---|---|
| Arduino Mega 2560 | 1 | — | on shelf | Genuine; pipe \\| inside |
| D1 Mini Pro | 2 | Box A | on shelf | |

## Bike / MTB parts

| Item | Qty | Location | Status | Bike | Notes |
|---|---|---|---|---|---|
| Brake pads | 4 | Garage | on shelf | Enduro | |
"""


class CustomPageTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        root = tmp / "Agents"
        root.mkdir()
        shutil.copytree(FIXTURE_ROOT / "Stash", root / "Stash")
        shutil.copytree(FIXTURE_ROOT / "Rocky", root / "Rocky")
        for patcher in (patch.object(agents, "AGENTS_ROOT", root), patch.object(vault, "AGENTS_ROOT", root)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.dir = root / "Stash"
        (self.dir / "page.json").unlink(missing_ok=True)
        (self.dir / "catalog.md").write_text(CATALOG, encoding="utf-8")

    def write(self, spec):
        (self.dir / "page.json").write_text(json.dumps(spec), encoding="utf-8")

    def test_no_page_means_none_and_check_explains_the_format(self):
        self.assertIsNone(custom_pages.load("stash"))
        self.assertIn('"type": "table"', custom_pages.check("stash"))

    def test_table_reads_every_markdown_table_under_its_heading(self):
        self.write({"title": "Inventory", "blocks": [{"type": "table", "file": "catalog.md", "search": True}]})
        page = custom_pages.load("stash")
        block = page["blocks"][0]
        self.assertEqual(page["title"], "Inventory")
        self.assertTrue(block["search"])
        self.assertEqual([s["heading"] for s in block["sections"]], ["Electronics components", "Bike / MTB parts"])
        self.assertEqual(block["sections"][0]["rows"][0], ["Arduino Mega 2560", "1", "—", "on shelf", "Genuine; pipe | inside"])
        self.assertEqual(block["sections"][1]["columns"][4], "Bike", "each table keeps its own columns")
        self.assertIn("3 rows in 2 sections", custom_pages.check("stash"))

    def test_markdown_block_leaves_out_front_matter(self):
        (self.dir / "plan-note.md").write_text("---\nStatus: active\n---\n\n# Plan\nStep one\n", encoding="utf-8")
        self.write({"blocks": [{"type": "markdown", "file": "plan-note.md"}]})
        text = custom_pages.load("stash")["blocks"][0]["text"]
        self.assertNotIn("Status: active", text)
        self.assertIn("# Plan", text)

    def test_a_catalog_edit_shows_on_the_next_load(self):
        self.write({"blocks": [{"type": "table", "file": "catalog.md"}]})
        with (self.dir / "catalog.md").open("a", encoding="utf-8") as f:
            f.write("| Chain | 1 | Garage | on shelf | Enduro | new |\n")
        self.assertEqual(len(custom_pages.load("stash")["blocks"][0]["sections"][1]["rows"]), 2)

    def test_columns_picks_and_orders(self):
        self.write({"blocks": [{"type": "table", "file": "catalog.md", "columns": ["Item", "Status"]}]})
        sec = custom_pages.load("stash")["blocks"][0]["sections"][0]
        self.assertEqual(sec["columns"], ["Item", "Status"])
        self.assertEqual(sec["rows"][1], ["D1 Mini Pro", "on shelf"])

    def test_bad_blocks_become_errors_not_a_blank_page(self):
        self.write({"blocks": [
            {"type": "table", "file": "../Rocky/mission.md"},
            {"type": "table", "file": "/etc/passwd"},
            {"type": "table", "file": "missing.md"},
            {"type": "chart"},
            {"type": "text", "text": "still here"},
        ]})
        blocks = custom_pages.load("stash")["blocks"]
        self.assertEqual([b["type"] for b in blocks], ["error", "error", "error", "error", "text"])
        self.assertIn("inside your own folder", blocks[0]["message"])
        self.assertIn("ERROR", custom_pages.check("stash"))

    def test_invalid_json_is_reported(self):
        (self.dir / "page.json").write_text("{nope", encoding="utf-8")
        self.assertIn("not valid JSON", custom_pages.load("stash")["blocks"][0]["message"])

    def test_write_page_tool_saves_only_valid_pages(self):
        refused = llm_client._run_tool("stash", "write_page", {"page": {"blocks": [{"type": "table", "file": "nope.md"}]}})
        self.assertTrue(refused.startswith("refused"))
        self.assertFalse((self.dir / "page.json").exists())
        saved = llm_client._run_tool("stash", "write_page", {"page": {"title": "Inventory", "blocks": [{"type": "table", "file": "catalog.md"}]}})
        self.assertIn("All good", saved)
        self.assertEqual(json.loads((self.dir / "page.json").read_text())["title"], "Inventory")
        self.assertIn("All good", llm_client._run_tool("stash", "check_page", {}))

    def test_html_block_is_served_only_when_the_page_uses_it(self):
        llm_client._run_tool("stash", "write_page", {"page": {"blocks": [{"type": "html", "file": "page.html"}]}, "html": "<p>hi</p>"})
        self.assertEqual(custom_pages.html_file("stash", "page.html"), (self.dir / "page.html").resolve())
        (self.dir / "other.html").write_text("<p>x</p>")
        self.assertIsNone(custom_pages.html_file("stash", "other.html"))

    def test_html_endpoint_is_sandboxed(self):
        import main
        from fastapi.testclient import TestClient
        llm_client._run_tool("stash", "write_page", {"page": {"blocks": [{"type": "html", "file": "page.html"}]}, "html": "<p>hi</p>"})
        client = TestClient(main.app)
        r = client.get("/stash/page/html/page.html")
        self.assertEqual(r.status_code, 200)
        self.assertIn("sandbox allow-scripts", r.headers["content-security-policy"])
        self.assertIn("default-src 'none'", r.headers["content-security-policy"])
        self.assertEqual(client.get("/stash/page/html/other.html").status_code, 404)
        self.assertEqual(client.get("/stash/page").json()["page"]["blocks"][0]["type"], "html")


BUYING = """# AliExpress List

| Item | Details | Status |
|---|---|---|
| LED strip | 24 V | Wanted |
"""


class PageFormTests(unittest.TestCase):
    """The form block: one text box + Add appends
    `- [ ] <text>` to the note the page names, and nothing else."""

    write = CustomPageTests.write

    def setUp(self):
        CustomPageTests.setUp(self)
        (self.dir / "aliexpress-list.md").write_text(BUYING, encoding="utf-8")
        self.write({"title": "Inventory", "blocks": [
            {"type": "table", "file": "catalog.md"},
            {"type": "form", "file": "aliexpress-list.md", "title": "Add to buying list"},
        ]})

    def read(self, name="aliexpress-list.md"):
        return (self.dir / name).read_text(encoding="utf-8")

    def test_add_appends_a_checklist_line_after_a_blank_line(self):
        result = custom_pages.add_item("stash", 1, "  USB-C   breakout board ")
        self.assertEqual(result["added"], "USB-C breakout board")
        self.assertEqual(self.read(), BUYING + "\n- [ ] USB-C breakout board\n")
        custom_pages.add_item("stash", 1, "Heat shrink")
        self.assertTrue(self.read().endswith("- [ ] USB-C breakout board\n- [ ] Heat shrink\n"), "next item joins the list")
        self.assertEqual([i["text"] for i in custom_pages.add_item("stash", 1, "Flux")["items"]],
                         ["USB-C breakout board", "Heat shrink", "Flux"])

    def test_page_shows_the_form_with_its_items(self):
        custom_pages.add_item("stash", 1, "Flux")
        form = custom_pages.load("stash")["blocks"][1]
        self.assertEqual((form["type"], form["button"], form["items"]), ("form", "Add", [{"text": "Flux", "done": False}]))
        self.assertIn("form adding", custom_pages.check("stash"))

    def test_invalid_text_is_refused_and_nothing_is_written(self):
        for bad in ("", "   ", "\n\t", None, 42, "x" * 301):
            with self.assertRaises(custom_pages.PageError, msg=repr(bad)):
                custom_pages.add_item("stash", 1, bad)
        self.assertEqual(self.read(), BUYING)

    def test_newlines_cannot_break_out_of_the_line(self):
        custom_pages.add_item("stash", 1, "Solder\n# Heading\r\n- [x] fake")
        self.assertTrue(self.read().endswith("\n- [ ] Solder # Heading - [x] fake\n"))

    def test_only_a_form_block_of_the_saved_page_can_be_written(self):
        for block in (0, 2, -1, 99, True, "1", None):
            with self.assertRaises(custom_pages.PageError, msg=repr(block)):
                custom_pages.add_item("stash", block, "x")
        self.assertEqual(self.read(), BUYING)
        (self.dir / "page.json").unlink()
        with self.assertRaises(custom_pages.PageError):
            custom_pages.add_item("stash", 1, "x")

    def test_forbidden_paths(self):
        outside = self.dir.parent / "Rocky" / "notes.md"
        outside.write_text("rocky\n", encoding="utf-8")
        for path in ("../Rocky/notes.md", "/etc/passwd", "mission.md", "persona.md", "backlog.md", "plan.md",
                     "page.json", "conversations/x.md", ".hidden.md", "new-file.md", "catalog.html"):
            self.write({"blocks": [{"type": "form", "file": path}]})
            with self.assertRaises(custom_pages.PageError, msg=path):
                custom_pages.add_item("stash", 0, "x")
            self.assertEqual(custom_pages.load("stash")["blocks"][0]["type"], "error", path)
        self.assertEqual(outside.read_text(encoding="utf-8"), "rocky\n")
        self.assertFalse((self.dir / "new-file.md").exists(), "never creates a file")

    def test_endpoint(self):
        import main
        from fastapi.testclient import TestClient
        client = TestClient(main.app)
        r = client.post("/stash/page/add", json={"block": 1, "text": "Flux pen"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["items"][-1]["text"], "Flux pen")
        self.assertIn("- [ ] Flux pen", self.read())
        r = client.post("/stash/page/add", json={"block": 1, "text": " "})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "type something first"))
        self.assertEqual(client.post("/stash/page/add", json={"block": 0, "text": "x"}).status_code, 400)
        self.assertEqual(client.post("/stash/page/add", json={"file": "mission.md", "text": "x"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()


class PageHistoryTests(unittest.TestCase):
    """Page versions: no agent silently replaces a page
    the owner uses; they get Keep / Revert, and "revert your page" works."""
    write = CustomPageTests.write

    def setUp(self):
        CustomPageTests.setUp(self)
        import page_history
        self.h = page_history

    def test_built_in_page_then_agent_page_waits_for_the_owner_and_reverts(self):
        self.h.snapshot("stash")
        self.assertEqual(self.h.versions("stash"), [], "nothing recorded while on the built-in page")
        self.write({"blocks": [{"type": "text", "text": "mine"}]})
        self.h.snapshot("stash")
        self.assertIsNone(self.h.pending("stash"), "the first version is the starting point")
        self.write({"blocks": [{"type": "text", "text": "rewritten"}]})
        self.h.snapshot("stash")
        self.h.snapshot("stash")
        self.assertEqual([v["v"] for v in self.h.versions("stash")], [1, 2], "unchanged: no new version")
        self.assertEqual(self.h.pending("stash")["kept_v"], 1)
        result = self.h.restore("stash")
        self.assertEqual(result["restored"], 1)
        self.assertIn("mine", (self.dir / "page.json").read_text())
        self.assertIsNone(self.h.pending("stash"))
        self.assertEqual(self.h.versions("stash")[-1]["note"], "restored v1")

    def test_keep(self):
        self.write({"blocks": [{"type": "text", "text": "a"}]}); self.h.snapshot("stash")
        self.write({"blocks": [{"type": "text", "text": "b"}]}); self.h.snapshot("stash")
        self.h.keep("stash")
        self.assertIsNone(self.h.pending("stash"))
        self.write({"blocks": [{"type": "text", "text": "c"}]}); self.h.snapshot("stash")
        self.h.restore("stash")
        self.assertIn('"b"', (self.dir / "page.json").read_text(), "back to the version the owner kept, not the oldest")

    def test_html_file_is_part_of_the_version_and_deleting_page_returns_to_built_in(self):
        (self.dir / "page.html").write_text("<p>v1</p>")
        self.write({"blocks": [{"type": "html", "file": "page.html"}]}); self.h.snapshot("stash")
        (self.dir / "page.html").write_text("<p>v2</p>"); self.h.snapshot("stash")
        self.assertEqual(len(self.h.versions("stash")), 2, "an html edit is a new version")
        self.h.restore("stash", v=1)
        self.assertEqual((self.dir / "page.html").read_text(), "<p>v1</p>")
        (self.dir / "page.json").unlink(); self.h.snapshot("stash")
        self.assertEqual(self.h.versions("stash")[-1]["hash"], "none")
        self.h.restore("stash", v=1)
        self.assertTrue((self.dir / "page.json").exists())

    def test_restore_to_built_in_removes_page_json(self):
        self.write({"blocks": [{"type": "text", "text": "a"}]}); self.h.snapshot("stash")
        index = self.h._read_index("stash")
        index["phone"].insert(0, {"v": 0, "hash": "none", "at": "2026-10-01T00:00:00+00:00", "kept": True, "note": ""})
        (self.h._version_dir("stash", "phone", 0)).mkdir(parents=True)
        (self.h._version_dir("stash", "phone", 0) / "files.json").write_text("{}")
        self.h._write_index("stash", index)
        self.h.restore("stash", v=0)
        self.assertFalse((self.dir / "page.json").exists(), "back to the app's built-in page")

    def test_desktop_versions(self):
        (self.dir / "dashboard.html").write_text("<p>one</p>"); self.h.snapshot("stash")
        (self.dir / "dashboard.html").write_text("<p>two</p>"); self.h.snapshot("stash")
        self.assertIsNone(self.h.pending("stash"), "no Keep on desktop")
        self.h.restore("stash", "desktop")
        self.assertEqual((self.dir / "dashboard.html").read_text(), "<p>one</p>")

    def test_errors_and_trimming(self):
        with self.assertRaises(ValueError):
            self.h.restore("stash")
        for i in range(self.h.MAX_VERSIONS + 5):
            self.write({"blocks": [{"type": "text", "text": f"t{i}"}]}); self.h.snapshot("stash")
        entries = self.h.versions("stash")
        self.assertEqual(len(entries), self.h.MAX_VERSIONS)
        self.assertEqual(entries[0]["v"], 1, "the version the owner kept is never trimmed")
        with self.assertRaises(ValueError):
            self.h.restore("stash", v=2)

    def test_history_is_hidden_from_files_and_pages(self):
        self.write({"blocks": [{"type": "text", "text": "a"}]}); self.h.snapshot("stash")
        self.assertTrue((self.dir / ".page-history" / "index.json").exists())
        self.assertFalse(any(".page-history" in f["path"] for f in vault.list_workspace_files("stash")))
        self.write({"blocks": [{"type": "markdown", "file": ".page-history/index.json"}]})
        self.assertEqual(custom_pages.load("stash")["blocks"][0]["type"], "error")

    def test_check_page_lists_versions_and_restore_tool(self):
        self.write({"blocks": [{"type": "text", "text": "a"}]})
        llm_client._run_tool("stash", "check_page", {})
        self.write({"blocks": [{"type": "text", "text": "b"}]})
        out = llm_client._run_tool("stash", "check_page", {})
        self.assertIn("v2 (now, waiting for the user)", out)
        self.assertIn("restore_page", out)
        out = llm_client._run_tool("stash", "restore_page", {})
        self.assertIn("v1 is back", out)
        self.assertIn('"a"', (self.dir / "page.json").read_text())
        self.assertIn("refused", llm_client._run_tool("stash", "restore_page", {"version": 99}))

    def test_endpoints(self):
        import main
        from fastapi.testclient import TestClient
        client = TestClient(main.app)
        self.write({"blocks": [{"type": "text", "text": "a"}]})
        self.assertIsNone(client.get("/stash/page").json()["changed"])
        self.write({"blocks": [{"type": "text", "text": "b"}]})
        self.assertEqual(client.get("/stash/page").json()["changed"]["kept_v"], 1)
        r = client.post("/stash/page/revert").json()
        self.assertEqual((r["restored"], r["changed"]), (1, None))
        self.assertEqual(r["page"]["blocks"][0]["text"], "a")
        self.write({"blocks": [{"type": "text", "text": "c"}]})
        client.get("/stash/page")
        client.post("/stash/page/keep")
        self.assertIsNone(client.get("/stash/page").json()["changed"])


class SlidersAndSectionsTests(unittest.TestCase):
    write = CustomPageTests.write

    def setUp(self):
        CustomPageTests.setUp(self)

    def test_sliders_block(self):
        self.write({"blocks": [{"type": "sliders", "title": "Pain", "metrics": [
            {"id": "lower_back", "label": "Lower back", "min": 0, "max": 10, "step": 1},
            {"id": "dorsiflexion", "min": 6, "max": 15, "step": 0.5, "unit": "cm"}]}]})
        b = custom_pages.load("stash")["blocks"][0]
        self.assertEqual((b["type"], b["section"], b["button"]), ("sliders", "Progress", "Log measurements"))
        self.assertEqual(b["metrics"][1], {"id": "dorsiflexion", "label": "dorsiflexion", "min": 6, "max": 15, "step": 0.5, "unit": "cm"})
        for bad in ([], [{"id": "Bad Id"}], [{"id": "x", "min": 5, "max": 1}], [{"id": "x", "step": 0}], [{"id": "x", "min": "a"}], "x"):
            self.write({"blocks": [{"type": "sliders", "metrics": bad}]})
            self.assertEqual(custom_pages.load("stash")["blocks"][0]["type"], "error", repr(bad))

    def test_table_sections_filter(self):
        self.write({"blocks": [{"type": "table", "file": "catalog.md", "sections": ["bike / mtb parts"]}]})
        b = custom_pages.load("stash")["blocks"][0]
        self.assertEqual([s["heading"] for s in b["sections"]], ["Bike / MTB parts"])
        self.write({"blocks": [{"type": "table", "file": "catalog.md", "sections": ["Nope"]}]})
        self.assertIn("no table under Nope", custom_pages.load("stash")["blocks"][0]["message"])
