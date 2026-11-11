"""Host header check against DNS rebinding (main.enforce_trusted_host)."""
import dataclasses
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


def trusted_for(**network) -> set[str]:
    config = dataclasses.replace(main.CONFIG, **network)
    with patch.object(main, "CONFIG", config), patch.object(main, "_lan_address", return_value="192.168.1.20"):
        return main._trusted_hosts(main._trusted_origins())


class HostCheckTests(unittest.TestCase):
    def test_loopback_and_ip_literals_pass_with_or_without_port(self):
        trusted = trusted_for(bind_address="127.0.0.1", public_url="", extra_origins=())
        for host in ("localhost", "localhost:8777", "127.0.0.1:8777", "[::1]:8777", "LOCALHOST:1",
                     "10.0.0.5:9000", "app.localhost:8777"):
            self.assertTrue(main.host_allowed(host, trusted), host)

    def test_rebound_names_are_refused(self):
        trusted = trusted_for(bind_address="0.0.0.0", public_url="https://box.example.ts.net",
                              extra_origins=("http://notes.lan:8777",))
        for host in ("attacker.example", "attacker.example:8777", "localhost.attacker.example", "", None,
                     "box.example.ts.net.attacker.example"):
            self.assertFalse(main.host_allowed(host, trusted), host)

    def test_configured_names_pass(self):
        trusted = trusted_for(bind_address="0.0.0.0", public_url="https://box.example.ts.net",
                              extra_origins=("http://notes.lan:8777",))
        for host in ("box.example.ts.net", "box.example.ts.net:443", "BOX.example.ts.net.", "notes.lan:8777",
                     "notes.lan", "192.168.1.20:8777"):
            self.assertTrue(main.host_allowed(host, trusted), host)

    def test_named_bind_address_passes(self):
        self.assertTrue(main.host_allowed("myhost.lan:8777", trusted_for(bind_address="myhost.lan")))

    def test_untrusted_host_gets_a_421_saying_what_to_set(self):
        client = TestClient(main.app)
        self.assertEqual(client.get("/api/version").status_code, 200)  # TestClient's own host
        response = client.get("/api/version", headers={"host": "rebound.example:8777"})
        self.assertEqual(response.status_code, 421)
        self.assertIn("public_url", response.json()["detail"])
        self.assertIn("extra_origins", response.json()["detail"])
        local = client.get("/api/version", headers={"host": f"127.0.0.1:{main.CONFIG.port}"})
        self.assertEqual(local.status_code, 200)

    def test_proxy_headers_do_not_bypass_the_check(self):
        client = TestClient(main.app)
        response = client.get("/api/version", headers={"host": "rebound.example", "x-forwarded-host": "localhost"})
        self.assertEqual(response.status_code, 421)


if __name__ == "__main__":
    unittest.main()
