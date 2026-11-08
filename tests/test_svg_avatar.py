import unittest

import svg_avatar

GOOD = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><linearGradient id="g"><stop offset="0" stop-color="#3a6"/></linearGradient></defs>
<circle cx="32" cy="32" r="30" fill="url(#g)"/><path d="M20 40 L32 20 L44 40 Z" fill="#fff"/></svg>"""


class SvgAvatarTests(unittest.TestCase):
    def test_keeps_plain_drawing(self):
        out = svg_avatar.sanitize(GOOD)
        for kept in ("<circle", "<path", "linearGradient", 'fill="url(#g)"', 'viewBox="0 0 64 64"'):
            self.assertIn(kept, out)

    def test_strips_scripts_handlers_links_and_styles(self):
        evil = """<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" onload="alert(1)">
<script>alert(1)</script><foreignObject><iframe src="x"/></foreignObject>
<a href="javascript:alert(1)"><circle r="5"/></a>
<image xlink:href="https://evil.example/x.png"/>
<rect width="10" height="10" style="fill:url(https://evil.example)" onclick="x()" fill="url(https://evil.example/p)"/>
<circle r="4" xlink:href="#x"/></svg>"""
        out = svg_avatar.sanitize(evil).lower()
        for gone in ("script", "onload", "onclick", "foreignobject", "iframe", "<a", "href", "<image", "style", "evil.example"):
            self.assertNotIn(gone, out)
        self.assertIn("<rect", out)

    def test_rejects_doctype_non_svg_invalid_and_oversized(self):
        for bad in ('<!DOCTYPE svg [<!ENTITY a "b">]><svg/>', "<html/>", "<svg><circle></svg>",
                    "<svg>" + " " * 25_000 + "</svg>"):
            with self.assertRaises(svg_avatar.InvalidSvg):
                svg_avatar.sanitize(bad)

    def test_adds_viewbox_and_namespace_when_missing(self):
        out = svg_avatar.sanitize('<svg><circle r="3"/></svg>')
        self.assertIn('viewBox="0 0 64 64"', out)
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', out)


if __name__ == "__main__":
    unittest.main()
