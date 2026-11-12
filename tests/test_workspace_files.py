import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agents
import vault

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "Agents"


class WorkspaceFilesTests(unittest.TestCase):
    """Files tab backend, against synthetic agent folders in a temp dir."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        root = tmp / "Agents"
        root.mkdir()
        shutil.copytree(FIXTURE_ROOT / "Spice", root / "Spice")
        (root / "Spice" / "conversations").mkdir()
        (root / "Spice" / "conversations" / "c.md").write_text("chat")
        (root / "Spice" / "memory").mkdir(exist_ok=True)
        (root / "Spice" / "memory" / "likes.md").write_text("jazz\n")
        (root / "Spice" / "data.json").write_text('{"a": 1}')
        shutil.copytree(FIXTURE_ROOT / "Rocky", root / "Rocky")
        for patcher in (patch.object(agents, "AGENTS_ROOT", root), patch.object(vault, "AGENTS_ROOT", root)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.dir = root / "Spice"

    def test_list_includes_structured_and_memory_files_but_not_conversations_or_dashboard(self):
        paths = [f["path"] for f in vault.list_workspace_files("spice")]
        self.assertIn("mission.md", paths)
        self.assertIn("memory/likes.md", paths)
        self.assertIn("data.json", paths)
        self.assertFalse(any(p.startswith("conversations/") for p in paths))
        self.assertNotIn("dashboard.html", paths)
        self.assertLess(paths.index("mission.md"), paths.index("memory/likes.md"))

    def test_roundtrip_and_conflict(self):
        f = vault.read_workspace_file("spice", "memory/likes.md")
        saved = vault.write_workspace_file("spice", "memory/likes.md", "jazz\ntrip-hop\n", f["version"])
        self.assertEqual((self.dir / "memory" / "likes.md").read_text(), "jazz\ntrip-hop\n")
        with self.assertRaises(vault.NoteConflictError):
            vault.write_workspace_file("spice", "memory/likes.md", "x", f["version"])  # stale
        vault.write_workspace_file("spice", "memory/likes.md", "x", saved["version"])

    def test_rejects_bad_paths_and_types(self):
        for bad in ("../Reba/mission.md", "/etc/passwd.md", "conversations/c.md", "dashboard.html", ".git/x.md", ""):
            with self.assertRaises(ValueError, msg=bad):
                vault.workspace_file("spice", bad)

    def test_symlink_cannot_escape_the_folder(self):
        """Phone Files button: no reads or writes outside
        the agent's folder, also through a symlink inside it."""
        outside = self.dir.parent / "Rocky" / "mission.md"
        before = outside.read_text()
        (self.dir / "sneaky.md").symlink_to(outside)
        (self.dir / "linked").symlink_to(self.dir.parent / "Rocky", target_is_directory=True)
        for path in ("sneaky.md", "linked/mission.md"):
            with self.assertRaises(ValueError, msg=path):
                vault.read_workspace_file("spice", path)
            with self.assertRaises(ValueError, msg=path):
                vault.write_workspace_file("spice", path, "pwned", None)
        self.assertEqual(outside.read_text(), before)

    def test_endpoints_refuse_escapes_and_stale_saves(self):
        import main
        from fastapi.testclient import TestClient
        client = TestClient(main.app)
        for path in ("../Rocky/mission.md", "/etc/passwd", "..%2FRocky%2Fmission.md", ".page-history/index.json"):
            self.assertEqual(client.get("/spice/files/content", params={"path": path}).status_code, 400, path)
            self.assertEqual(client.put("/spice/files/content", json={"path": path, "content": "x", "version": "v"}).status_code, 400, path)
        f = client.get("/spice/files/content", params={"path": "memory/likes.md"}).json()
        self.assertEqual(client.put("/spice/files/content", json={"path": "memory/likes.md", "content": "a\n", "version": f["version"]}).status_code, 200)
        stale = client.put("/spice/files/content", json={"path": "memory/likes.md", "content": "b\n", "version": f["version"]})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual((self.dir / "memory" / "likes.md").read_text(), "a\n", "the stale save changed nothing")

    def test_does_not_create_new_files(self):
        with self.assertRaises(FileNotFoundError):
            vault.write_workspace_file("spice", "new.md", "hi", None)

    def test_invalid_json_and_identity_limit_refused(self):
        v = vault.read_workspace_file("spice", "data.json")["version"]
        with self.assertRaises(ValueError):
            vault.write_workspace_file("spice", "data.json", "{nope", v)
        v = vault.read_workspace_file("spice", "mission.md")["version"]
        with self.assertRaises(ValueError):
            vault.write_workspace_file("spice", "mission.md", "x" * 5000, v)

    def test_create_new_file(self):
        f = vault.create_workspace_file("spice", "memory/recipes")
        self.assertEqual(f["path"], "memory/recipes.md")
        self.assertTrue((self.dir / "memory" / "recipes.md").is_file())
        vault.write_workspace_file("spice", f["path"], "pasta\n", f["version"])
        with self.assertRaises(FileExistsError):
            vault.create_workspace_file("spice", "memory/recipes.md")
        for bad in ("../x.md", "conversations/x.md", "evil.html"):
            with self.assertRaises(ValueError, msg=bad):
                vault.create_workspace_file("spice", bad)
        self.assertEqual(vault.read_workspace_file("spice", vault.create_workspace_file("spice", "d.json")["path"])["content"], "{}")

    def test_rename_keeps_extension_and_refuses_clashes(self):
        self.assertEqual(vault.rename_workspace_file("spice", "memory/likes.md", "memory/loves")["path"], "memory/loves.md")
        self.assertEqual((self.dir / "memory" / "loves.md").read_text(), "jazz\n")
        self.assertFalse((self.dir / "memory" / "likes.md").exists())
        with self.assertRaises(FileExistsError):
            vault.rename_workspace_file("spice", "memory/loves.md", "data.json")
        with self.assertRaises(ValueError):
            vault.rename_workspace_file("spice", "memory/loves.md", "memory/loves.md")

    def test_core_files_cannot_be_renamed_moved_or_deleted(self):
        listed = {f["path"]: f["protected"] for f in vault.list_workspace_files("spice")}
        self.assertTrue(listed["mission.md"])
        self.assertFalse(listed["memory/likes.md"])
        with self.assertRaises(ValueError):
            vault.rename_workspace_file("spice", "mission.md", "old-mission.md")
        with self.assertRaises(ValueError):
            vault.rename_workspace_file("spice", "memory/likes.md", "plan.md")
        with self.assertRaises(ValueError):
            vault.delete_workspace_file("spice", "backlog.md")
        with self.assertRaises(ValueError):
            vault.move_workspace_file("spice", "rocky", "persona.md")
        self.assertTrue((self.dir / "mission.md").exists())

    def test_delete(self):
        vault.delete_workspace_file("spice", "data.json")
        self.assertFalse((self.dir / "data.json").exists())
        with self.assertRaises(FileNotFoundError):
            vault.delete_workspace_file("spice", "data.json")

    def test_move_to_other_agent(self):
        vault.move_workspace_file("spice", "rocky", "memory/likes.md")
        self.assertFalse((self.dir / "memory" / "likes.md").exists())
        moved = vault.agent_root("rocky") / "memory" / "likes.md"
        self.assertEqual(moved.read_text(), "jazz\n")
        (self.dir / "memory" / "likes.md").write_text("again")
        with self.assertRaises(FileExistsError):
            vault.move_workspace_file("spice", "rocky", "memory/likes.md")
        self.assertEqual(moved.read_text(), "jazz\n")
        with self.assertRaises(ValueError):
            vault.move_workspace_file("spice", "spice", "memory/likes.md")


if __name__ == "__main__":
    unittest.main()
