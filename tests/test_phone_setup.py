"""Phone setup (phone_setup.py) against a fake `tailscale`, and the one-key
TOML edit (toml_edit.py) it and the installer use. Never runs the real
tailscale: `_tailscale` is patched to a stub script in a temp folder."""
import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import phone_setup
import toml_edit

STUB = r"""#!/usr/bin/env bash
# Fake tailscale. Scenario files in $TS_DIR decide the answers.
echo "$*" >> "$TS_DIR/calls"
case "$1 $2 $3" in
  "status --json "*) cat "$TS_DIR/status.json"; exit 0 ;;
  "serve status --json") cat "$TS_DIR/serve.json" 2>/dev/null || echo '{}'; exit 0 ;;
  "serve --bg "*)
    if [ -f "$TS_DIR/deny" ]; then echo "Access denied: serve config denied" >&2; exit 1; fi
    printf '{"Web":{"box.tail1234.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:%s"}}}}}' "$3" > "$TS_DIR/serve.json"
    exit 0 ;;
esac
exit 1
"""

RUNNING = ('{"BackendState":"Running","Self":{"DNSName":"box.tail1234.ts.net."},'
           '"CurrentTailnet":{"MagicDNSEnabled":true},"CertDomains":["box.tail1234.ts.net"]}')


class FakeTailscale(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.stub = self.dir / "tailscale"
        self.stub.write_text(STUB)
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IXUSR)
        (self.dir / "status.json").write_text(RUNNING)
        self.toml = self.dir / "openclave.toml"
        self.toml.write_text('# my settings\n[owner]\nname = "Alex"\n')
        env = mock.patch.dict(os.environ, {"TS_DIR": str(self.dir)})
        env.start()
        self.addCleanup(env.stop)
        which = mock.patch.object(phone_setup, "_tailscale", return_value=str(self.stub))
        which.start()
        self.addCleanup(which.stop)

    def calls(self):
        path = self.dir / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def test_status_reads_only(self):
        st = phone_setup.status(8777)
        self.assertTrue(st["installed"] and st["logged_in"] and st["https_certs"] and st["magic_dns"])
        self.assertEqual(st["dns_name"], "box.tail1234.ts.net")
        self.assertFalse(any(c.startswith("serve --bg") for c in self.calls()))

    def test_setup_serves_and_saves_public_url(self):
        result = phone_setup.setup(8777, self.toml)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["url"], "https://box.tail1234.ts.net")
        self.assertEqual(result["mobile_url"], "https://box.tail1234.ts.net/mobile/")
        self.assertIn("serve --bg 8777", self.calls())
        text = self.toml.read_text()
        self.assertIn("# my settings", text)
        self.assertEqual(tomllib.loads(text)["network"]["public_url"], "https://box.tail1234.ts.net")

    def test_already_served_is_not_served_again(self):
        (self.dir / "serve.json").write_text(
            '{"Web":{"box.tail1234.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8777"}}}}}')
        self.assertTrue(phone_setup.setup(8777, self.toml)["ok"])
        self.assertFalse(any(c.startswith("serve --bg") for c in self.calls()))

    def test_never_replaces_another_serve(self):
        (self.dir / "serve.json").write_text(
            '{"Web":{"box.tail1234.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:3000"}}}}}')
        result = phone_setup.setup(8777, self.toml)
        self.assertEqual(result["reason"], "busy")
        self.assertIn("3000", result["message"])
        self.assertFalse(any(c.startswith("serve --bg") for c in self.calls()))
        self.assertNotIn("public_url", self.toml.read_text())

    def test_permission_failure_gives_the_commands(self):
        (self.dir / "deny").write_text("")
        result = phone_setup.setup(8790, self.toml)
        self.assertEqual(result["reason"], "needs_root")
        self.assertEqual(result["commands"], ["sudo tailscale serve --bg 8790", "sudo tailscale set --operator=$USER"])
        self.assertNotIn("public_url", self.toml.read_text())

    def test_https_off_says_what_to_turn_on(self):
        (self.dir / "status.json").write_text(RUNNING.replace('"CertDomains":["box.tail1234.ts.net"]', '"CertDomains":null'))
        result = phone_setup.setup(8777, self.toml)
        self.assertEqual(result["reason"], "https_off")
        self.assertIn("MagicDNS", result["message"])
        self.assertIn("HTTPS Certificates", result["message"])
        self.assertFalse(any(c.startswith("serve --bg") for c in self.calls()))

    def test_logged_out(self):
        (self.dir / "status.json").write_text('{"BackendState":"NeedsLogin"}')
        self.assertEqual(phone_setup.setup(8777, self.toml)["reason"], "logged_out")
        self.assertEqual(phone_setup.main(["status", "--port", "8777"]), 11)

    def test_not_installed(self):
        with mock.patch.object(phone_setup, "_tailscale", return_value=None):
            self.assertEqual(phone_setup.setup(8777, self.toml)["reason"], "not_installed")
            self.assertEqual(phone_setup.main(["status"]), 10)

    def test_cli_exit_codes(self):
        self.assertEqual(phone_setup.main(["status", "--port", "8777"]), 0)
        (self.dir / "deny").write_text("")
        with mock.patch("builtins.print"):
            self.assertEqual(phone_setup.main(["setup", "--port", "8777", "--toml", str(self.toml)]), 3)

    def test_saved_public_url(self):
        self.assertEqual(phone_setup.saved_public_url(self.toml), "")
        self.toml.write_text('[network]\npublic_url = "https://x.ts.net/"\n')
        self.assertEqual(phone_setup.saved_public_url(self.toml), "https://x.ts.net")
        self.assertEqual(phone_setup.saved_public_url(self.dir / "missing.toml"), "")


class TomlEditTests(unittest.TestCase):
    def test_adds_section_when_missing(self):
        text = '# OpenClave settings\n[owner]\nname = "Alex"  # me\n\n'
        out = toml_edit.set_key_text(text, "network", "public_url", "https://a.ts.net")
        self.assertTrue(out.startswith('# OpenClave settings\n[owner]\nname = "Alex"  # me\n'))
        self.assertTrue(out.endswith('\n\n[network]\npublic_url = "https://a.ts.net"\n'))

    def test_adds_key_inside_existing_section_before_the_next(self):
        text = '[network]\n# bind = "0.0.0.0"\nport = 8790\n\n[time]\ntimezone = "UTC"\n'
        out = toml_edit.set_key_text(text, "network", "public_url", "https://a.ts.net")
        self.assertEqual(out, '[network]\n# bind = "0.0.0.0"\nport = 8790\npublic_url = "https://a.ts.net"\n\n[time]\ntimezone = "UTC"\n')
        self.assertEqual(tomllib.loads(out)["time"]["timezone"], "UTC")

    def test_replaces_existing_key_only(self):
        text = '[network]\npublic_url = "https://old.ts.net"   \nport = 8790\n[owner]\npublic_url_note = 1\n'
        out = toml_edit.set_key_text(text, "network", "public_url", "https://new.ts.net")
        parsed = tomllib.loads(out)
        self.assertEqual(parsed["network"], {"public_url": "https://new.ts.net", "port": 8790})
        self.assertEqual(parsed["owner"], {"public_url_note": 1})

    def test_empty_file_and_types(self):
        self.assertEqual(toml_edit.set_key_text("", "network", "port", 8778), "[network]\nport = 8778\n")
        self.assertEqual(toml_edit.set_key_text("", "updates", "check", False), "[updates]\ncheck = false\n")
        self.assertIn('\\"', toml_edit.set_key_text("", "owner", "name", 'A "B"'))

    def test_refuses_what_it_cannot_do_safely(self):
        with self.assertRaises(ValueError):
            toml_edit.set_key_text('network = { port = 1 }\n', "network", "port", 2)
        with self.assertRaises(ValueError):
            toml_edit.set_key_text("", "net work", "port", 2)
        with self.assertRaises(tomllib.TOMLDecodeError):
            toml_edit.set_key_text("[network\n", "network", "port", 2)

    def test_set_key_keeps_mode_and_creates_file(self):
        d = Path(tempfile.mkdtemp())
        f = d / "openclave.toml"
        toml_edit.set_key(f, "network", "port", 8800)
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        f.chmod(0o640)
        toml_edit.set_key(f, "network", "port", 8801)
        self.assertEqual(f.stat().st_mode & 0o777, 0o640)
        self.assertEqual(tomllib.loads(f.read_text())["network"]["port"], 8801)
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["openclave.toml"])
        self.assertEqual(toml_edit.main([str(f), "network", "port", "8802", "--int"]), 0)
        self.assertEqual(tomllib.loads(f.read_text())["network"]["port"], 8802)


if __name__ == "__main__":
    unittest.main()
