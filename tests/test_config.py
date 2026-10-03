"""Unit tests for scripts/controld_config.py (standard library only)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import controld_config as cc  # noqa: E402


def _config(**overrides):
    base = {
        "lists": [
            {
                "file": "example-folder.json",
                "targets": [{"profile": "P", "folder": "F"}],
            }
        ]
    }
    base.update(overrides)
    return base


class ParseConfigTests(unittest.TestCase):
    def test_example_config_is_valid(self):
        config = cc.load_config(str(REPO_ROOT / "config.example.toml"))
        self.assertEqual(
            config.target_files,
            ["apple-private-relay-allow-folder.json", "spam-tlds-folder.json"],
        )
        self.assertEqual(config.max_delete_percent, cc.DEFAULT_MAX_DELETE_PERCENT)
        self.assertEqual(config.mirror_fallback, cc.DEFAULT_MIRROR_FALLBACK)

    def test_defaults_when_settings_omitted(self):
        config = cc.parse_config(_config())
        self.assertEqual(config.max_delete_percent, 50)
        self.assertTrue(config.mirror_fallback)
        self.assertEqual(config.lists[0].targets, (("P", "F"),))

    def test_settings_override(self):
        config = cc.parse_config(
            _config(settings={"max_delete_percent": 10, "mirror_fallback": False})
        )
        self.assertEqual(config.max_delete_percent, 10)
        self.assertFalse(config.mirror_fallback)

    def test_order_is_preserved(self):
        data = _config()
        data["lists"].append(
            {"file": "a-folder.json", "targets": [{"profile": "P", "folder": "G"}]}
        )
        self.assertEqual(
            cc.parse_config(data).target_files, ["example-folder.json", "a-folder.json"]
        )

    def test_rejects_missing_or_empty_lists(self):
        for data in ({}, {"lists": []}, {"lists": "x"}):
            with self.assertRaises(cc.ConfigError):
                cc.parse_config(data)

    def test_rejects_unsafe_filenames(self):
        for name in ("../x.json", "a/b.json", ".hidden.json", "x.txt", "", "x.json/"):
            data = _config()
            data["lists"][0]["file"] = name
            with self.assertRaises(cc.ConfigError, msg=name):
                cc.parse_config(data)

    def test_rejects_duplicate_file(self):
        data = _config()
        data["lists"].append(
            {"file": "example-folder.json", "targets": [{"profile": "P", "folder": "G"}]}
        )
        with self.assertRaises(cc.ConfigError):
            cc.parse_config(data)

    def test_rejects_folder_fed_by_two_files(self):
        data = _config()
        data["lists"].append(
            {"file": "other-folder.json", "targets": [{"profile": "P", "folder": "F"}]}
        )
        with self.assertRaises(cc.ConfigError):
            cc.parse_config(data)

    def test_same_folder_name_in_different_profiles_is_fine(self):
        data = _config()
        data["lists"][0]["targets"].append({"profile": "Q", "folder": "F"})
        self.assertEqual(len(cc.parse_config(data).lists[0].targets), 2)

    def test_rejects_bad_targets(self):
        for targets in ([], "x", [{"profile": "P"}], [{"profile": "", "folder": "F"}],
                        [{"profile": "P", "folder": "F", "extra": 1}]):
            data = _config()
            data["lists"][0]["targets"] = targets
            with self.assertRaises(cc.ConfigError, msg=repr(targets)):
                cc.parse_config(data)

    def test_rejects_bad_settings(self):
        for settings in ({"max_delete_percent": 0}, {"max_delete_percent": 101},
                         {"max_delete_percent": True}, {"max_delete_percent": "50"},
                         {"mirror_fallback": "yes"}, {"typo": 1}):
            with self.assertRaises(cc.ConfigError, msg=repr(settings)):
                cc.parse_config(_config(settings=settings))

    def test_rejects_unknown_top_level_key(self):
        with self.assertRaises(cc.ConfigError):
            cc.parse_config(_config(list=[]))


class LoadConfigTests(unittest.TestCase):
    def test_missing_file_points_to_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cc.ConfigError) as ctx:
                cc.load_config(os.path.join(tmp, "config.toml"))
        self.assertIn("config.example.toml", str(ctx.exception))

    def test_invalid_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            Path(path).write_text("this is = = not toml", encoding="utf-8")
            with self.assertRaises(cc.ConfigError):
                cc.load_config(path)

    def test_env_var_selects_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "custom.toml")
            Path(path).write_text(
                '[[lists]]\nfile = "x-folder.json"\n'
                'targets = [{ profile = "P", folder = "F" }]\n',
                encoding="utf-8",
            )
            old = os.environ.get(cc.CONFIG_ENV_VAR)
            os.environ[cc.CONFIG_ENV_VAR] = path
            try:
                self.assertEqual(cc.load_config().target_files, ["x-folder.json"])
            finally:
                if old is None:
                    del os.environ[cc.CONFIG_ENV_VAR]
                else:
                    os.environ[cc.CONFIG_ENV_VAR] = old


if __name__ == "__main__":
    unittest.main()
