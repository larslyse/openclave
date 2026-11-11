import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import deploy_state


class DeployStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "main.py").write_text("v1")
        patcher = patch.object(deploy_state, "ROOT", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        running = patch.object(deploy_state, "_RUNNING_HASHES", deploy_state._py_hashes())
        running.start()
        self.addCleanup(running.stop)

    def test_unchanged_code_is_not_stale(self):
        self.assertFalse(deploy_state.status()["stale"])

    def test_edited_and_new_files_are_reported(self):
        (self.tmp / "main.py").write_text("v2")
        (self.tmp / "new_module.py").write_text("x")
        status = deploy_state.status()
        self.assertTrue(status["stale"])
        self.assertEqual(status["changed_files"], ["main.py", "new_module.py"])

    def test_route_modules_count(self):
        (self.tmp / "routes").mkdir()
        (self.tmp / "routes" / "agents.py").write_text("x")
        self.assertEqual(deploy_state.status()["changed_files"], ["routes/agents.py"])

    def test_static_files_do_not_count(self):
        (self.tmp / "dashboard.html").write_text("changed")
        self.assertFalse(deploy_state.status()["stale"])


if __name__ == "__main__":
    unittest.main()
