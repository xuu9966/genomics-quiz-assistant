"""config.py / AppConfig 用例：可读错误、密码不落盘、环境变量、脱敏视图。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import config
from fixtures import config_copy
from models import AppConfig

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _clean_env(extra=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("CQA_")}
    env.update(extra or {})
    return env


class TestConfigValidation(unittest.TestCase):
    def test_missing_file_gives_readable_error_without_traceback(self) -> None:
        with mock.patch.dict(os.environ, _clean_env(), clear=True):
            with self.assertRaises(config.ConfigError) as ctx:
                AppConfig.load(os.path.join(tempfile.gettempdir(), "cqa-no-such-config.json"))
        self.assertIn("配置文件不存在", str(ctx.exception))

    def test_corrupted_file_gives_readable_error(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{ not a json ")
            with self.assertRaises(config.ConfigError) as ctx:
                AppConfig.load(path, require_password=False)
        self.assertIn("格式错误", str(ctx.exception))

    def test_missing_username_gives_readable_error(self) -> None:
        data = config_copy()
        data["username"] = ""
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate_config(data, require_password=False)
        self.assertIn("用户名", str(ctx.exception))

    def test_missing_base_url_gives_readable_error(self) -> None:
        data = config_copy()
        data["base_url"] = ""
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate_config(data, require_password=False)
        self.assertIn("base_url", str(ctx.exception))

    def test_illegal_chapters_gives_readable_error(self) -> None:
        data = config_copy()
        data["chapters"] = "2_测序"
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate_config(data, require_password=False)
        self.assertIn("chapters", str(ctx.exception))

    def test_missing_password_gives_readable_error(self) -> None:
        data = config_copy()
        data["password"] = ""
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate_config(data, require_password=True)
        self.assertIn("密码", str(ctx.exception))


class TestPasswordHandling(unittest.TestCase):
    def test_password_never_written_to_disk(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.json")
            cfg = AppConfig._from_dict(config_copy())
            cfg.password = "虚构密码-DO-NOT-LEAK"
            cfg.save(path)
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            self.assertNotIn("password\"", text.replace("password_env", ""))
            self.assertNotIn("虚构密码-DO-NOT-LEAK", text)
            self.assertEqual(json.loads(text).get("password"), None)

    def test_getpass_used_when_env_absent(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.json")
            AppConfig._from_dict(config_copy()).save(path)
            with mock.patch.dict(os.environ, _clean_env(), clear=True), \
                 mock.patch("config.getpass.getpass", return_value="虚构内存密码") as prompt:
                cfg = AppConfig.load(path)
            prompt.assert_called_once()
            self.assertEqual(cfg.password, "虚构内存密码")

    def test_env_password_preferred_without_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.json")
            AppConfig._from_dict(config_copy()).save(path)
            env = _clean_env({"CQA_PASSWORD": "虚构环境变量密码"})
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch("config.getpass.getpass") as prompt:
                cfg = AppConfig.load(path)
            prompt.assert_not_called()
            self.assertEqual(cfg.password, "虚构环境变量密码")

    def test_safe_dict_masks_all_sensitive_fields(self) -> None:
        cfg = AppConfig._from_dict(config_copy())
        cfg.password = "虚构密码"
        safe = cfg.safe_dict()
        self.assertEqual(safe["password"], "***")
        self.assertNotIn("虚构密码", json.dumps(safe, ensure_ascii=False))


class TestEnvOverrides(unittest.TestCase):
    def test_env_overrides_apply_to_load(self) -> None:
        env = _clean_env({
            "CQA_BASE_URL": "http://example.invalid:6000/",
            "CQA_LIMIT": "4",
            "CQA_CHAPTERS": "2_测序,自主学习",
            "CQA_WEB_PORT": "9001",
            "CQA_REQUEST_INTERVAL_SEC": "1.5",
        })
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.json")
            AppConfig._from_dict(config_copy()).save(path)
            with mock.patch.dict(os.environ, env, clear=True):
                cfg = AppConfig.load(path, require_password=False)
        self.assertEqual(cfg.base_url, "http://example.invalid:6000/")
        self.assertEqual(cfg.limit, 4)
        self.assertEqual(cfg.chapters, ["2_测序", "自主学习"])
        self.assertEqual(cfg.web["port"], 9001)
        self.assertEqual(cfg.request_interval_sec, 1.5)

    def test_template_has_no_password(self) -> None:
        self.assertNotIn("password", config.template())


class TestCliOffline(unittest.TestCase):
    def test_main_help_runs_without_third_party(self) -> None:
        result = subprocess.run(
            [sys.executable, "main.py", "--help"], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("run", result.stdout)
        self.assertIn("复习", result.stdout)


if __name__ == "__main__":
    unittest.main()
