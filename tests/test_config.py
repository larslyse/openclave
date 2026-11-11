"""Configuration loading stays self-contained and safe by default."""
import tempfile
import unittest
from pathlib import Path

from config import load_config


class ConfigTests(unittest.TestCase):
    def test_defaults_are_local_and_voice_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "openclave-home"
            settings = load_config(environ={"OPENCLAVE_HOME": str(home)})

        self.assertEqual(settings.home, home.resolve())
        self.assertEqual(settings.vault_root, home.resolve())
        self.assertEqual(settings.agents_root, home.resolve() / "agents")
        self.assertEqual(settings.port, 8777)
        self.assertEqual(settings.bind_address, "127.0.0.1")
        self.assertFalse(settings.public_url)
        self.assertFalse(settings.voice_python)
        self.assertIsNone(settings.voice_model_dir)

    def test_runtime_state_paths_are_under_state_and_create_the_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_config(environ={"OPENCLAVE_HOME": str(Path(tmp) / "data")})
            target = settings.state_path("agent_models.json")
            self.assertEqual(target, settings.home / "state" / "agent_models.json")
            self.assertTrue(target.parent.is_dir())

    def test_relative_paths_are_resolved_under_openclave_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "data"
            home.mkdir()
            config_file = home / "openclave.toml"
            config_file.write_text(
                '[paths]\nvault_root = "notes"\nagents_root = "agents"\n'
                '[network]\npublic_url = "https://example.test/openclave"\n'
                '[time]\ntimezone = "Europe/Oslo"\n',
                encoding="utf-8",
            )
            settings = load_config(environ={"OPENCLAVE_HOME": str(home)})

        self.assertEqual(settings.vault_root, home / "notes")
        self.assertEqual(settings.agents_root, home / "agents")
        self.assertEqual(settings.public_url, "https://example.test/openclave")
        self.assertEqual(settings.timezone, "Europe/Oslo")

    def test_note_languages(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "openclave.toml").write_text('[owner]\nnote_languages = ["English", " Norwegian "]\n')
            self.assertEqual(load_config(environ={"OPENCLAVE_HOME": str(home)}).note_languages, ("English", "Norwegian"))
            self.assertEqual(load_config(environ={"OPENCLAVE_HOME": str(Path(tmp) / "x")}).note_languages, ())
            (home / "openclave.toml").write_text('[owner]\nnote_languages = "English"\n')
            with self.assertRaises(ValueError):
                load_config(environ={"OPENCLAVE_HOME": str(home)})

    def test_extra_origins(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "openclave.toml").write_text('[network]\nextra_origins = ["http://host.test:8767/"]\n', encoding="utf-8")
            self.assertEqual(load_config(environ={"OPENCLAVE_HOME": str(home)}).extra_origins, ("http://host.test:8767",))
            (home / "openclave.toml").write_text('[network]\nextra_origins = ["nope"]\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "extra_origins"):
                load_config(environ={"OPENCLAVE_HOME": str(home)})

    def test_update_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(load_config(environ={"OPENCLAVE_HOME": str(home)}).update_channel, "release")
            (home / "openclave.toml").write_text('[updates]\nchannel = "main"\n', encoding="utf-8")
            self.assertEqual(load_config(environ={"OPENCLAVE_HOME": str(home)}).update_channel, "main")
            (home / "openclave.toml").write_text('[updates]\nchannel = "nightly"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "updates.channel"):
                load_config(environ={"OPENCLAVE_HOME": str(home)})

    def test_invalid_port_and_timezone_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config_file = home / "openclave.toml"
            config_file.write_text("[network]\nport = 70000\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "network.port"):
                load_config(environ={"OPENCLAVE_HOME": str(home)})
            config_file.write_text('[time]\ntimezone = "Not/AZone"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown timezone"):
                load_config(environ={"OPENCLAVE_HOME": str(home)})


if __name__ == "__main__":
    unittest.main()
