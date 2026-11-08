"""Upgrading stock agent page copies (agent_factory.upgrade_stock_dashboards, 2026-11-05)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_factory
import agents
from agents import AGENTS


class DashboardUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p = patch.object(agents, "AGENTS_ROOT", Path(self.temp.name))
        p.start()
        self.addCleanup(p.stop)
        self.cfg = AGENTS["stash"]
        (Path(self.temp.name) / self.cfg.folder).mkdir(parents=True)
        self.f = Path(self.temp.name) / self.cfg.folder / "dashboard.html"

    def render(self):
        return agent_factory.render_dashboard(self.cfg.name, self.cfg.id, self.cfg.color, "ST")

    def test_an_older_stock_copy_is_upgraded_and_backed_up(self):
        old = self.render().replace("/static/vendor/marked-15.0.12.min.js", "https://cdn.jsdelivr.net/npm/marked/marked.min.js")
        old = old.replace("    --accent-dark: " + self.cfg.color, "    --accent-dark: #123456")
        old = old.replace("  </div>\n</header>", '    <div class="usage-line" id="usage-line"></div>\n  </div>\n</header>', 1)
        self.f.write_text(old)
        with patch.dict(AGENTS, {"stash": self.cfg}, clear=True):
            self.assertEqual(agent_factory.upgrade_stock_dashboards(), ["stash"])
            self.assertEqual(self.f.read_text(), self.render())
            self.assertEqual((self.f.parent / "dashboard.html.bak").read_text(), old)
            self.assertEqual(agent_factory.upgrade_stock_dashboards(), [], "current copies are left as they are")

    def test_a_custom_page_is_never_touched(self):
        custom = self.render().replace("</body>", "<div id='my-chart'>Weekly load</div>\n</body>")
        self.f.write_text(custom)
        with patch.dict(AGENTS, {"stash": self.cfg}, clear=True):
            self.assertEqual(agent_factory.upgrade_stock_dashboards(), [])
        self.assertEqual(self.f.read_text(), custom)


if __name__ == "__main__":
    unittest.main()
