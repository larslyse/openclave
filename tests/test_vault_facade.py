"""Safety net for splitting vault.py: every name the module had before the
split (fixtures/vault_surface.json) is still reachable as vault.<name>, and
functions and classes re-exported from the vault_* modules are the same
objects there, so callers, isinstance checks and patches keep working."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import vault
from agents import AGENTS, AgentConfig

SURFACE_FILE = Path(__file__).parent / "fixtures" / "vault_surface.json"


class VaultFacadeTests(unittest.TestCase):
    def test_every_name_from_before_the_split_is_still_there(self):
        before = json.loads(SURFACE_FILE.read_text())
        missing = [name for name in before if not hasattr(vault, name)]
        self.assertEqual(missing, [])

    def test_re_exported_functions_and_classes_are_the_same_objects(self):
        for name in json.loads(SURFACE_FILE.read_text()):
            obj = getattr(vault, name)
            module = getattr(obj, "__module__", None)
            if callable(obj) and module and module.startswith("vault_") and module != "vault_access":
                with self.subTest(name=name):
                    self.assertIs(getattr(sys.modules[module], name), obj)

    def test_patched_roots_on_the_facade_reach_the_moved_code(self):
        greg = AgentConfig("greg", "Greg", "Greg", "#6a8f60", "Synthetic test agent")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(AGENTS, {"greg": greg}), \
                patch.object(vault, "AGENTS_ROOT", Path(tmp).resolve()):
            root = Path(tmp).resolve() / greg.folder
            self.assertEqual(vault.agent_root("greg"), root)
            self.assertEqual(vault.conversations_dir("greg"), root / "conversations")


if __name__ == "__main__":
    unittest.main()
