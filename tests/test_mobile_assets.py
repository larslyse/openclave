"""Mobile opens fast: no third-party scripts in the way, and every local
asset and avatar is cacheable through a content-version `?v=`."""
import re
import unittest

import main
from routes import library as library_routes


class MobileAssetTests(unittest.TestCase):
    def test_mobile_page_loads_nothing_from_other_hosts(self):
        html = main.mobile_dashboard().body.decode()
        self.assertNotRegex(html, r'(src|href)="https?://', "an outside CDN blocks app start on a slow lookup")

    def test_every_local_script_and_stylesheet_is_versioned(self):
        html = main.mobile_dashboard().body.decode()
        assets = re.findall(r'<(?:script|link rel="stylesheet")[^>]*(?:src|href)="(/static/[^"]+)"', html)
        self.assertGreaterEqual(len(assets), 6)
        for url in assets:
            self.assertRegex(url, r"\?v=\d+$", f"{url} would be re-downloaded on every open")

    def test_versioned_avatar_is_cached_and_others_are_not(self):
        agent = next(a for a in main.AGENTS if library_routes._avatar_file(a))
        v = library_routes._avatar_version(agent)
        self.assertIn("immutable", library_routes.agent_avatar(agent, v=v).headers["cache-control"])
        self.assertEqual(library_routes.agent_avatar(agent).headers["cache-control"], "no-store")
        self.assertEqual(library_routes.agent_avatar(agent, v=v - 1).headers["cache-control"], "no-store", "stale version")

    def test_agent_list_carries_avatar_version(self):
        listed = {a.id: a.avatar_v for a in main.api_agents()}
        for agent_id, v in listed.items():
            self.assertEqual(v, library_routes._avatar_version(agent_id))


if __name__ == "__main__":
    unittest.main()
