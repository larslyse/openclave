"""Optional token login (access.py, 2026-11-05)."""
import dataclasses
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import access
import main


class AccessTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(access.enabled())
        self.assertEqual(TestClient(main.app).get("/api/version").status_code, 200)

    def test_token_needed_once_then_a_cookie(self):
        with patch.object(access, "CONFIG", dataclasses.replace(access.CONFIG, access_token="s3cret-token")):
            client = TestClient(main.app)  # client host "testclient": not loopback
            self.assertEqual(client.get("/api/version").status_code, 401)
            page = client.get("/mobile/", headers={"accept": "text/html"}, follow_redirects=False)
            self.assertEqual((page.status_code, page.headers["location"]), (303, "/login?next=/mobile/"))
            self.assertEqual(client.post("/login", data={"token": "wrong"}).status_code, 401)
            ok = client.post("/login?next=/mobile/", data={"token": "s3cret-token"}, follow_redirects=False)
            self.assertEqual((ok.status_code, ok.headers["location"]), (303, "/mobile/"))
            self.assertNotIn("s3cret-token", ok.headers["set-cookie"], "the cookie holds a hash")
            self.assertEqual(client.get("/api/version").status_code, 200)
            evil = client.post("/login?next=//evil.example", data={"token": "s3cret-token"}, follow_redirects=False)
            self.assertEqual(evil.headers["location"], "/")

    def test_loopback_without_proxy_header_is_trusted(self):
        with patch.object(access, "CONFIG", dataclasses.replace(access.CONFIG, access_token="t")):
            self.assertTrue(access.allowed("127.0.0.1", {}, {}, "/api/version"))
            self.assertFalse(access.allowed("127.0.0.1", {"x-forwarded-for": "100.1.2.3"}, {}, "/api/version"))
            self.assertFalse(access.allowed("203.0.113.5", {}, {}, "/api/version"))


if __name__ == "__main__":
    unittest.main()
