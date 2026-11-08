from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_order
from agents import AgentConfig


FAKE_AGENTS = {
    "a": AgentConfig(id="a", name="A", folder="A", color="#000", tagline=""),
    "b": AgentConfig(id="b", name="B", folder="B", color="#000", tagline=""),
    "c": AgentConfig(id="c", name="C", folder="C", color="#000", tagline=""),
}


class AgentOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.order_file = Path(self.temp.name) / "agent_order.json"
        self._patches = [
            patch.object(agent_order, "ORDER_FILE", self.order_file),
            patch.object(agent_order, "AGENTS", FAKE_AGENTS),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.temp.cleanup()

    def test_default_order_is_registry_order_when_nothing_saved(self):
        self.assertEqual(agent_order.get_order(), ["a", "b", "c"])

    def test_saved_order_round_trips(self):
        agent_order.set_order(["c", "a", "b"])
        self.assertEqual(agent_order.get_order(), ["c", "a", "b"])

    def test_set_order_rejects_unknown_or_repeated_ids(self):
        with self.assertRaises(ValueError):
            agent_order.set_order(["a", "b", "c", "d"])  # unknown "d"
        with self.assertRaises(ValueError):
            agent_order.set_order(["a", "a", "b"])

    def test_partial_order_reorders_only_those_agents(self):
        # Boxes showing one clave (claves.py) sends just its agents.
        agent_order.set_order(["a", "b", "c"])
        agent_order.set_order(["c", "a"])
        self.assertEqual(agent_order.get_order(), ["c", "b", "a"])

    def test_agent_added_after_save_is_appended_not_dropped(self):
        agent_order.set_order(["c", "a", "b"])
        with patch.object(agent_order, "AGENTS", {**FAKE_AGENTS, "d": AgentConfig(
            id="d", name="D", folder="D", color="#000", tagline="",
        )}):
            self.assertEqual(agent_order.get_order(), ["c", "a", "b", "d"])

    def test_corrupt_order_file_falls_back_to_registry_order(self):
        self.order_file.write_text("not json")
        self.assertEqual(agent_order.get_order(), ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
