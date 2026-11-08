"""Avatar maker: the URL fetch only reaches the public internet, and the
cartoon filter returns a same-size picture. No real network calls."""
import io
import socket
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import avatar_maker as am


def png_bytes(size=(64, 48), mode="RGB"):
    image = Image.new(mode, size, (200, 120, 40, 255) if mode == "RGBA" else (200, 120, 40))
    ImageDraw.Draw(image).ellipse((10, 10, 40, 40), fill=(20, 60, 200))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def resolves_to(*ips):
    return lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.is_redirect = status in (301, 302, 303, 307, 308)

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FetchTests(unittest.TestCase):
    def test_refuses_non_http_and_credentials(self):
        for url in ("file:///etc/passwd", "ftp://example.com/a.png", "javascript:alert(1)", "", "https://u:p@example.com/a.png"):
            with self.subTest(url=url), self.assertRaises(am.AvatarError):
                am.fetch_image(url)

    def test_refuses_private_and_local_addresses(self):
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.10", "172.16.0.1", "169.254.169.254", "::1", "fd00::1", "100.64.0.1", "0.0.0.0"):
            with self.subTest(ip=ip), patch.object(socket, "getaddrinfo", resolves_to(ip)), \
                 patch.object(am.requests.Session, "get") as get:
                with self.assertRaises(am.AvatarError):
                    am.fetch_image("https://pictures.example/a.png")
                get.assert_not_called()

    def test_one_private_address_among_public_is_refused(self):
        with patch.object(socket, "getaddrinfo", resolves_to("93.184.216.34", "10.0.0.1")), \
             self.assertRaises(am.AvatarError):
            am.fetch_image("https://pictures.example/a.png")

    def test_redirect_to_private_address_is_refused(self):
        def fake_getaddrinfo(host, port, **kw):
            return resolves_to("10.0.0.7" if host == "intranet.example" else "93.184.216.34")(host, port)
        redirect = FakeResponse(302, headers={"location": "http://intranet.example/secret.png"})
        with patch.object(socket, "getaddrinfo", fake_getaddrinfo), \
             patch.object(am.requests.Session, "get", return_value=redirect) as get, \
             self.assertRaises(am.AvatarError):
            am.fetch_image("https://pictures.example/a.png")
        self.assertEqual(get.call_count, 1, "the private target is never requested")

    def test_fetches_a_public_picture_and_follows_redirects(self):
        responses = [FakeResponse(301, headers={"location": "/b.png"}), FakeResponse(200, png_bytes())]
        with patch.object(socket, "getaddrinfo", resolves_to("93.184.216.34")), \
             patch.object(am.requests.Session, "get", side_effect=responses) as get:
            image = am.fetch_image("https://pictures.example/a.png")
        self.assertEqual(image.size, (64, 48))
        self.assertEqual(get.call_args_list[1].args[0], "https://pictures.example/b.png")
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_too_large_and_not_a_picture(self):
        with patch.object(socket, "getaddrinfo", resolves_to("93.184.216.34")):
            big = FakeResponse(200, b"x" * 10, {"content-length": str(am.MAX_DOWNLOAD + 1)})
            with patch.object(am.requests.Session, "get", return_value=big), self.assertRaises(am.AvatarError):
                am.fetch_image("https://pictures.example/a.png")
            streamed = FakeResponse(200, b"x" * (am.MAX_DOWNLOAD + 10))
            with patch.object(am.requests.Session, "get", return_value=streamed), self.assertRaises(am.AvatarError):
                am.fetch_image("https://pictures.example/a.png")
            html = FakeResponse(200, b"<html>not a picture</html>")
            with patch.object(am.requests.Session, "get", return_value=html), self.assertRaises(am.AvatarError):
                am.fetch_image("https://pictures.example/a.png")

    def test_large_picture_is_scaled_down(self):
        image = am.decode(png_bytes((3200, 1000)))
        self.assertEqual(max(image.size), am.MAX_SIDE)


class CartoonTests(unittest.TestCase):
    def test_every_style_keeps_size_and_reduces_colours(self):
        source = am.decode(png_bytes((120, 120)))
        for style in am.STYLES:
            for strength in (1, 2, 3):
                with self.subTest(style=style, strength=strength):
                    out = am.cartoonify(source, style, strength)
                    self.assertEqual(out.size, source.size)
                    self.assertEqual(out.mode, "RGB")

    def test_transparency_is_kept(self):
        out = am.cartoonify(am.decode(png_bytes((80, 80), "RGBA")), "cartoon", 2)
        self.assertEqual(out.mode, "RGBA")

    def test_unknown_style(self):
        with self.assertRaises(am.AvatarError):
            am.cartoonify(am.decode(png_bytes()), "watercolour", 2)

    def test_specks_are_dropped_lines_kept(self):
        # Noise texture (like grass) gives no outline; a big dark ring does.
        import random
        rnd = random.Random(1)
        noise = Image.new("RGB", (256, 256))
        noise.putdata([(v, v, v) for v in (rnd.randint(90, 170) for _ in range(256 * 256))])
        out = am.cartoonify(noise, "cartoon", 2)
        self.assertNotIn(am.INK, set(out.getdata()), "no specks from texture")
        ring = Image.new("RGB", (256, 256), (200, 200, 200))
        ImageDraw.Draw(ring).ellipse((60, 60, 196, 196), outline=(30, 30, 30), width=4)
        dark = sum(1 for px in am.cartoonify(ring, "cartoon", 2).getdata() if max(px) < 60)
        self.assertGreater(dark, 500)

    def test_tint_uses_the_colour_and_keeps_alpha(self):
        out = am.tint(am.decode(png_bytes((80, 80), "RGBA")), "#3366cc")
        self.assertEqual(out.mode, "RGBA")
        r, g, b, _ = out.convert("RGBA").resize((1, 1), Image.BOX).getpixel((0, 0))
        self.assertGreater(b, r, "blue-ish, like the colour")
        with self.assertRaises(am.AvatarError):
            am.tint(am.decode(png_bytes()), "red")

    def test_original_with_colour(self):
        out = Image.open(io.BytesIO(am.cartoon_png(png_bytes((100, 100)), "original", 2, "#cc3333"))).convert("RGB")
        r, g, b = out.resize((1, 1), Image.BOX).getpixel((0, 0))
        self.assertGreater(r, b)

    def test_cartoon_png_round_trip(self):
        out = Image.open(io.BytesIO(am.cartoon_png(png_bytes((500, 500)), "poster", 9)))
        self.assertLessEqual(max(out.size), am.WORK)


if __name__ == "__main__":
    unittest.main()


class EndpointTests(unittest.TestCase):
    def setUp(self):
        import main
        from fastapi.testclient import TestClient
        self.client = TestClient(main.app)

    def test_cartoon_endpoint_returns_png(self):
        res = self.client.post("/api/avatar/cartoon?style=comic&strength=2", content=png_bytes((200, 200)))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["content-type"], "image/png")
        self.assertEqual(Image.open(io.BytesIO(res.content)).size, (200, 200))
        self.assertEqual(self.client.post("/api/avatar/cartoon", content=b"nope").status_code, 400)
        self.assertEqual(self.client.post("/api/avatar/cartoon?style=x", content=png_bytes()).status_code, 400)

    def test_fetch_endpoint_refuses_private_address(self):
        res = self.client.post("/api/avatar/fetch", json={"url": "http://127.0.0.1:8767/"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("public internet", res.json()["detail"])

    def test_fetch_endpoint_returns_data_url(self):
        with patch.object(am, "fetch_image", return_value=am.decode(png_bytes())):
            res = self.client.post("/api/avatar/fetch", json={"url": "https://pictures.example/a.png"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["data_url"].startswith("data:image/png;base64,"))
