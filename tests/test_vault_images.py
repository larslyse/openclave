"""save_image (vault_images.py): product photos into a vault _assets folder. Temp vault, no network."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import llm_client
import vault
import vault_access
import vault_images

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 5000
PNG = b"\x89PNG\r\n\x1a\n" + b"y" * 5000
PAGE = b"""<html><head><meta property="og:image" content="/img/wera.jpg">
<title>Wera 8100</title></head><body>hi</body></html>"""
LD_PAGE = b"""<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@graph":[{"@type":"Product","name":"X","image":["https://cdn.example.com/x.png"]}]}
</script></head></html>"""


def fake_web(pages: dict):
    """_get replacement: url -> (content type, body)."""
    def _get(url, max_bytes):
        if url not in pages:
            raise vault_images.ImageError(f"HTTP 404 from {url}")
        ctype, body = pages[url]
        return httpx.Response(200, headers={"content-type": ctype}, content=body), body, url
    return _get


class SaveImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.root = root
        for p in (patch.object(vault, "VAULT_ROOT", root), patch.object(vault, "AGENTS_ROOT", root / "OpenClave" / "Agents")):
            p.start()
            self.addCleanup(p.stop)
        vault.agent_root("rick").mkdir(parents=True)
        (vault.agent_root("rick") / "agent.json").write_text(json.dumps({"id": "rick"}))
        (root / "Area" / "Buy" / "_assets").mkdir(parents=True)
        (root / "Area" / "Other").mkdir(parents=True)
        vault_access.set_folders("rick", ["Area/Buy"])

    def save(self, url, note="Area/Buy/Tools.md", name="", pages=None):
        with patch.object(vault_images, "_get", fake_web(pages or {})):
            return vault_images.save("rick", url, note, name)

    def test_image_url_saved_before_the_note_exists(self):
        out = self.save("https://shop.example.com/a.jpg", name="Wera 8100 SA6",
                        pages={"https://shop.example.com/a.jpg": ("image/jpeg", JPEG)})
        self.assertEqual(out["embed"], "![[wera-8100-sa6.jpg]]")
        self.assertEqual((self.root / "Area/Buy/_assets/wera-8100-sa6.jpg").read_bytes(), JPEG)
        self.assertTrue(out["new"])

    def test_product_page_uses_og_image_then_structured_data(self):
        out = self.save("https://maker.example.com/p", name="wera", pages={
            "https://maker.example.com/p": ("text/html; charset=utf-8", PAGE),
            "https://maker.example.com/img/wera.jpg": ("image/jpeg", JPEG)})
        self.assertEqual(out["source"], "https://maker.example.com/p")
        out = self.save("https://maker.example.com/q", name="x", pages={
            "https://maker.example.com/q": ("text/html", LD_PAGE),
            "https://cdn.example.com/x.png": ("image/png", PNG)})
        self.assertEqual(out["embed"], "![[x.png]]")

    def test_same_image_twice_is_reused_different_image_gets_suffix(self):
        pages = {"https://s.example.com/a.jpg": ("image/jpeg", JPEG), "https://s.example.com/b.jpg": ("image/jpeg", JPEG + b"z")}
        self.save("https://s.example.com/a.jpg", name="pick", pages=pages)
        self.assertFalse(self.save("https://s.example.com/a.jpg", name="pick", pages=pages)["new"])
        self.assertEqual(self.save("https://s.example.com/b.jpg", name="pick", pages=pages)["embed"], "![[pick-2.jpg]]")

    def test_refusals(self):
        img = {"https://s.example.com/a.jpg": ("image/jpeg", JPEG)}
        cases = [
            ("https://s.example.com/a.jpg", "Area/Other/x.md", img, "outside your vault folders"),
            ("https://s.example.com/a.jpg", "OpenClave/Agents/rick/x.md", img, "agent"),
            ("https://s.example.com/a.svg", "Area/Buy/x.md",
             {"https://s.example.com/a.svg": ("image/svg+xml", b"<svg onload=alert(1)>" + b" " * 2000)}, "not a supported image"),
            ("https://s.example.com/t.gif", "Area/Buy/x.md",
             {"https://s.example.com/t.gif": ("image/gif", b"GIF89a" + b"\x00" * 30)}, "tiny"),
            ("https://s.example.com/p", "Area/Buy/x.md",
             {"https://s.example.com/p": ("text/html", b"<html><head></head></html>")}, "no main image"),
        ]
        for url, note, pages, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(vault_images.ImageError) as ctx:
                    self.save(url, note=note, pages=pages)
                self.assertIn(expected, str(ctx.exception))
        self.assertEqual(list((self.root / "Area/Buy/_assets").iterdir()), [])

    def test_needs_an_assets_folder_and_never_makes_one(self):
        (self.root / "Area/Buy/_assets").rmdir()
        with self.assertRaises(vault_images.ImageError) as ctx:
            self.save("https://s.example.com/a.jpg", pages={"https://s.example.com/a.jpg": ("image/jpeg", JPEG)})
        self.assertIn("_assets", str(ctx.exception))
        self.assertFalse((self.root / "Area/Buy/_assets").exists())

    def test_no_folders_no_images(self):
        vault_access.set_folders("rick", [])
        with self.assertRaises(vault_images.ImageError) as ctx:
            self.save("https://s.example.com/a.jpg")
        self.assertIn("no vault folders", str(ctx.exception))

    def test_private_and_odd_hosts_refused(self):
        for url in ("http://127.0.0.1/a.jpg", "http://192.168.1.10/a.jpg", "file:///etc/passwd", "ftp://x.example.com/a"):
            with self.subTest(url=url):
                with self.assertRaises(vault_images.ImageError):
                    vault_images._check_host(url)

    def test_bot_protection_reported(self):
        r = httpx.Response(403, headers={"cf-mitigated": "challenge"}, content=b"<title>Just a moment...</title>")
        self.assertTrue(vault_images._blocked(r, r.content))
        r = httpx.Response(403, content=b"plain forbidden")
        self.assertFalse(vault_images._blocked(r, r.content))

    def test_tool_is_registered_and_replies(self):
        import agent_tools
        self.assertIn("save_image", agent_tools.OPTIONAL_TOOLS)
        self.assertTrue(any(t["function"]["name"] == "save_image" for t in llm_client.TOOLS))
        with patch.object(vault_images, "_get", fake_web({"https://s.example.com/a.jpg": ("image/jpeg", JPEG)})):
            out = llm_client._run_tool("rick", "save_image", {"url": "https://s.example.com/a.jpg",
                                                              "note_path": "Area/Buy/Tools.md", "name": "a"})
        self.assertTrue(out.startswith("Success!"), out)
        self.assertIn("![[a.jpg]]", out)
        out = llm_client._run_tool("rick", "save_image", {"url": "http://10.0.0.1/a.jpg", "note_path": "Area/Buy/T.md"})
        self.assertTrue(out.startswith("refused:"), out)


if __name__ == "__main__":
    unittest.main()
