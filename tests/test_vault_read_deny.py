"""The read deny rule: agents never read OpenClave's keys, settings, state,
dotfiles or confidential folders, through any read or search path."""
import dataclasses
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import llm_client  # noqa: E402
import pdf_export  # noqa: E402
import retrieval  # noqa: E402
import vault  # noqa: E402

SECRET = "SECRET_SENTINEL"


class DenyRuleBase(unittest.TestCase):
    """A default-layout install: vault_root = OPENCLAVE_HOME."""
    vault_subdir = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "home"
        self.root = self.home / self.vault_subdir if self.vault_subdir else self.home
        self.agents = self.root / "agents"
        for rel in ("agents/rick", "Area", "Work", ".obsidian", ".trash", "state/garmin"):
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        (self.home / "state").mkdir(parents=True, exist_ok=True)
        files = {
            self.home / ".env": f"ANTHROPIC_API_KEY={SECRET}",
            self.home / "openclave.toml": f'[network]\naccess_token = "{SECRET}"\n',
            self.home / "private-strings.txt": SECRET,
            self.home / "state" / "notes.md": SECRET,
            self.root / "Work" / "Plan.md": SECRET,
            self.root / ".obsidian" / "workspace.md": SECRET,
            self.root / ".trash" / "Old.md": SECRET,
            self.root / "Area" / "id_ed25519": SECRET,
            self.root / "Area" / "server.pem": SECRET,
            self.root / "Area" / "Note.md": "fine",
            self.agents / "rick" / "mission.md": "mission",
        }
        for f, text in files.items():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
        self.config = dataclasses.replace(
            vault.CONFIG, home=self.home, vault_root=self.root, agents_root=self.agents,
            confidential_folders=("Work",), garmin_token_dir=self.home / "state" / "garmin",
            qmd_cache_home=self.home / "state" / "qmd-cache", qmd_config_home=self.home / "state" / "qmd-config")
        for p in (patch.object(vault, "CONFIG", self.config), patch.object(vault, "VAULT_ROOT", self.root),
                  patch.object(vault, "AGENTS_ROOT", self.agents), patch.object(vault, "QMD_ENV", self.config.qmd_env),
                  patch.object(pdf_export, "CONFIDENTIAL_FOLDERS", (self.root / "Work",)),
                  patch.object(pdf_export, "CONFIDENTIAL", self.root / "Work")):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, path) -> str:
        return llm_client._run_tool("rick", "read_vault_file", {"path": str(path)})


class AbsolutePathReads(DenyRuleBase):
    def test_keys_settings_state_dotfiles_and_secrets_are_refused(self):
        for f in (self.home / ".env", self.home / "openclave.toml", self.home / "private-strings.txt",
                  self.home / "state" / "notes.md", self.root / ".obsidian" / "workspace.md",
                  self.root / ".trash" / "Old.md", self.root / "Area" / "id_ed25519",
                  self.root / "Area" / "server.pem"):
            result = self.read(f)
            self.assertNotIn(SECRET, result, f)
            # Outside a separate notes folder the older "outside vault" check answers first.
            self.assertTrue(result.startswith("refused:") or "outside vault" in result, (f, result))

    def test_confidential_folder_read_names_the_folder(self):
        result = self.read(self.root / "Work" / "Plan.md")
        self.assertNotIn(SECRET, result)
        self.assertIn("'Work' is a confidential folder", result)

    def test_symlink_to_a_secret_is_refused(self):
        link = self.root / "Area" / "innocent.md"
        link.symlink_to(self.home / ".env")
        self.assertNotIn(SECRET, self.read(link))
        link2 = self.root / "Area" / "plan.md"
        link2.symlink_to(self.root / "Work" / "Plan.md")
        self.assertIn("confidential", self.read(link2))

    def test_normal_notes_and_agent_files_still_read(self):
        self.assertEqual(self.read(self.root / "Area" / "Note.md"), "fine")
        self.assertEqual(self.read(self.agents / "rick" / "mission.md"), "mission")

    def test_writes_into_state_are_refused(self):
        if not self.vault_subdir:
            with self.assertRaises(vault.MainVaultError):
                vault.main_vault_target("state/new.md")
        with self.assertRaises(vault.MainVaultError):
            vault.main_vault_target("Work/new.md")
        self.assertEqual(vault.main_vault_target("Area/new.md"), self.root / "Area" / "new.md")

    def test_pdf_export_skips_state_and_secrets(self):
        self.assertFalse(pdf_export._allowed(self.home / "state" / "notes.md"))
        self.assertTrue(pdf_export._allowed(self.root / "Area" / "Note.md"))
        with self.assertRaises(pdf_export.PdfError):
            pdf_export.resolve_note("notes")  # only match is state/notes.md


class SeparateNotesFolderLayout(AbsolutePathReads):
    """vault_root points at a notes folder (an Obsidian vault) of its own."""
    vault_subdir = "Notes"


class QmdForms(DenyRuleBase):
    def test_uri_refusal_for_denied_folders(self):
        roots = {"notes": self.root}
        self.assertIn("confidential", vault.qmd_uri_refusal("qmd://notes/work/plan.md:3 #abc", roots))
        self.assertIn("confidential", vault.qmd_uri_refusal("notes/Work/Plan.md", roots))
        self.assertIsNotNone(vault.qmd_uri_refusal("qmd://notes/state/notes.md", roots))
        self.assertIsNotNone(vault.qmd_uri_refusal("qmd://notes/.obsidian/x.md", roots))
        self.assertIsNotNone(vault.qmd_uri_refusal("qmd://notes/openclave.toml", roots))
        self.assertIsNone(vault.qmd_uri_refusal("qmd://notes/area/note.md", roots))
        self.assertIsNone(vault.qmd_uri_refusal("qmd://notes/workshop/note.md", roots))
        # A collection rooted inside a denied folder is denied as a whole.
        self.assertIsNotNone(vault.qmd_uri_refusal("qmd://w/plan.md", {"w": self.root / "Work"}))

    def test_get_by_uri_refuses_before_calling_qmd(self):
        with patch.object(vault, "qmd_available", return_value=True), \
                patch.object(vault.subprocess, "run", side_effect=AssertionError("qmd called")):
            self.assertIn("confidential", self.read("qmd://notes/work/plan.md"))

    def test_get_result_matching_only_denied_documents_is_refused(self):
        # qmd resolves docids and path suffixes itself, so the returned text is
        # checked against the index too.
        db = vault._qmd_db_file()
        db.parent.mkdir(parents=True, exist_ok=True)
        import sqlite3
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE documents (collection TEXT, path TEXT, hash TEXT, active INT)")
            con.execute("CREATE TABLE content (hash TEXT, doc TEXT)")
            con.execute("INSERT INTO documents VALUES ('notes', 'work/plan.md', 'h1', 1)")
            con.execute("INSERT INTO content VALUES ('h1', ?)", (SECRET + "\n",))
            con.execute("INSERT INTO documents VALUES ('notes', 'area/note.md', 'h2', 1)")
            con.execute("INSERT INTO content VALUES ('h2', 'fine')")
        with patch.object(vault, "qmd_available", return_value=True), patch.object(vault.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = SECRET + "\n"
            self.assertIn("confidential", self.read("#h1abcd"))
            run.return_value.stdout = "fine\n"
            self.assertEqual(self.read("#h2abcd"), "fine")

    def test_search_hits_in_denied_folders_are_dropped(self):
        output = ("qmd://notes/work/plan.md:1 #a\nsecret plan\n\n"
                  "qmd://notes/state/notes.md:1 #b\nstate\n\n"
                  "qmd://notes/area/note.md:1 #c\nfine")
        with patch.object(vault, "qmd_query", return_value=output), \
                patch.object(vault, "qmd_available", return_value=True):
            out = self.read_search("plan")
        self.assertIn("area/note.md", out)
        self.assertNotIn("work/plan.md", out)
        self.assertNotIn("state/notes.md", out)

    def read_search(self, query):
        return llm_client._run_tool("rick", "search_vault", {"query": query})

    def test_auto_lookup_drops_denied_hits(self):
        output = "qmd://notes/work/plan.md:1 #a\nsecret\n\nqmd://notes/area/note.md:1 #c\nfine"
        self.assertEqual([b.split()[0] for b in retrieval._drop_excluded_hits(output)],
                         ["qmd://notes/area/note.md:1"])

    def test_ignore_list_is_added_to_every_collection_and_kept(self):
        f = vault._qmd_config_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(yaml.safe_dump({"global_context": "x", "collections": {
            "notes": {"path": str(self.root), "pattern": "**/*.md", "ignore": ["Drafts/**"]},
            "work": {"path": str(self.root / "Work"), "pattern": "**/*.md"},
            "elsewhere": {"path": str(Path(self.tmp.name) / "other"), "pattern": "**/*.md"}}}))
        vault._qmd_apply_ignores()
        data = yaml.safe_load(f.read_text())
        self.assertEqual(data["global_context"], "x")
        notes = data["collections"]["notes"]["ignore"]
        self.assertEqual(notes[0], "Drafts/**")
        self.assertIn("Work/**", notes)
        self.assertIn("state/**", notes)
        self.assertNotIn("state/garmin/**", notes)  # covered by state/**
        self.assertEqual(data["collections"]["work"]["ignore"], ["**"])
        self.assertNotIn("ignore", data["collections"]["elsewhere"])
        before = f.read_text()
        vault._qmd_apply_ignores()
        self.assertEqual(f.read_text(), before)


@unittest.skipUnless(shutil.which(vault.QMD_BIN), "qmd is not installed")
class RealQmdIndex(DenyRuleBase):
    """With the real qmd binary on a temp index: denied folders leave the index,
    so docids and path guesses can't reach them either."""

    def test_refresh_drops_denied_documents(self):
        vault._qmd_last_refresh = 0.0
        self.addCleanup(setattr, vault, "_qmd_last_refresh", 0.0)
        with patch.object(vault, "QMD_INDEX", "deny-test"):
            vault._qmd_refresh_index(timeout=60)
            listed = subprocess.run([vault.QMD_BIN, "--index", "deny-test", "ls", "notes"],
                                    capture_output=True, text=True, env={**vault.os.environ, **vault.QMD_ENV}).stdout
            self.assertIn("area/note.md", listed)
            self.assertNotIn("work/plan.md", listed)
            self.assertNotIn("state/notes.md", listed)
            self.assertNotIn(SECRET, vault.qmd_get("plan.md"))
            self.assertNotIn(SECRET, vault.qmd_get("notes.md"))


if __name__ == "__main__":
    unittest.main()
