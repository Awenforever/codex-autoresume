import os
import tempfile
import unittest


class TestConfig(unittest.TestCase):
    def test_defaults_load_without_file(self):
        from codex_autoresume.config import load_config
        cfg = load_config(os.path.join(tempfile.gettempdir(), "nonexistent-cxr.toml"))
        self.assertTrue(cfg["general"]["enabled"])
        self.assertEqual(cfg["resume"]["mode"], "exec")
        self.assertEqual(cfg["general"]["weekly_resume"], "off")
        self.assertTrue(cfg["update"]["enabled"])
        self.assertEqual(cfg["update"]["check_sec"], 86400)

    def test_unknown_key_rejected(self):
        from codex_autoresume.config import ConfigError, load_config
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write("[general]\nenabledd = true\n")
            path = f.name
        try:
            with self.assertRaises(ConfigError):
                load_config(path)
        finally:
            os.unlink(path)

    def test_merge_and_validate(self):
        from codex_autoresume.config import load_config
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False,
                                         encoding="utf-8") as f:
            f.write('[general]\nenabled = false\nweekly_resume = "per-thread"\n'
                    '[threads."01a05ddb-0483"]\nmodel = "gpt-6.1-sol"\n'
                    'reasoning_effort = "high"\nweekly_resume = true\n')
            path = f.name
        try:
            cfg = load_config(path)
        finally:
            os.unlink(path)
        self.assertFalse(cfg["general"]["enabled"])
        self.assertEqual(cfg["general"]["weekly_resume"], "per-thread")
        self.assertEqual(cfg["threads"]["01a05ddb-0483"]["model"], "gpt-6.1-sol")

    def test_update_section_accepted(self):
        from codex_autoresume.config import load_config
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False,
                                         encoding="utf-8") as f:
            f.write('[update]\nrepo = "C:/tmp/checkout"\n'
                    'check_sec = 600\nplugin_spec = ""\n')
            path = f.name
        try:
            cfg = load_config(path)
        finally:
            os.unlink(path)
        self.assertEqual(cfg["update"]["repo"], "C:/tmp/checkout")
        self.assertEqual(cfg["update"]["check_sec"], 600)
        self.assertEqual(cfg["update"]["plugin_spec"], "")
        self.assertTrue(cfg["update"]["enabled"])

    def test_invalid_enum(self):
        from codex_autoresume.config import ConfigError, load_config
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[general]\nweekly_resume = "sometimes"\n')
            path = f.name
        try:
            with self.assertRaises(ConfigError):
                load_config(path)
        finally:
            os.unlink(path)

    def test_thread_config_lookup(self):
        import copy
        from codex_autoresume.config import thread_config
        cfg = copy.deepcopy(__import__("codex_autoresume.config",
                                       fromlist=["DEFAULTS"]).DEFAULTS)
        cfg["threads"]["01a05ddb"] = {"model": "gpt-6.1-sol"}
        tc = thread_config(cfg, "01a05ddb-0483-7ea1-bf01-b298b386bff8")
        self.assertEqual(tc["model"], "gpt-6.1-sol")
        tc2 = thread_config(cfg, "ffffffff-0000")
        self.assertIsNone(tc2["model"])


if __name__ == "__main__":
    unittest.main()
