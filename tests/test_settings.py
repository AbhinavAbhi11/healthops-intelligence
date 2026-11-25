
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from settings import Settings, SettingsError


class SettingsTests(unittest.TestCase):
    def make_env(self, **overrides):
        env = {
            "HEALTHOPS_ENV": "test",
            "HEALTHOPS_HOST": "127.0.0.1",
            "HEALTHOPS_PORT": "8765",
            "HEALTHOPS_MAX_UPLOAD_MB": "100",
            "HEALTHOPS_LOG_LEVEL": "INFO",
            "HEALTHOPS_DATA_DIR": "instance",
            "LLM_PROVIDER": "gemini",
            "GEMINI_MODEL": "gemini-test-model",
            "HEALTHOPS_LLM_ENABLED": "true",
            "HEALTHOPS_LLM_TIMEOUT_SECONDS": "30",
        }
        env.update(overrides)
        return env

    def test_defaults(self):
        settings = Settings.from_env(self.make_env())
        self.assertEqual(settings.port, 8765)
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.max_upload_bytes, 100 * 1024 * 1024)

    def test_non_loopback_binding_rejected(self):
        with self.assertRaises(SettingsError):
            Settings.from_env(self.make_env(HEALTHOPS_HOST="0.0.0.0"))

    def test_production_mode_rejected(self):
        with self.assertRaisesRegex(SettingsError, "Production mode is disabled"):
            Settings.from_env(self.make_env(HEALTHOPS_ENV="production"))

    def test_invalid_port_rejected(self):
        with self.assertRaises(SettingsError):
            Settings.from_env(self.make_env(HEALTHOPS_PORT="70000"))

    def test_invalid_upload_size_rejected(self):
        with self.assertRaises(SettingsError):
            Settings.from_env(self.make_env(HEALTHOPS_MAX_UPLOAD_MB="0"))

    def test_invalid_boolean_rejected(self):
        with self.assertRaises(SettingsError):
            Settings.from_env(self.make_env(HEALTHOPS_LLM_ENABLED="maybe"))

    def test_relative_data_path(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.from_env(
                self.make_env(HEALTHOPS_DATA_DIR="private-data"),
                base_dir=Path(directory),
            )
        self.assertEqual(
            settings.data_dir,
            Path(directory).resolve() / "private-data",
        )


if __name__ == "__main__":
    unittest.main()
