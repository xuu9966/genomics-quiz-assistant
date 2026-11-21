"""合规与偏离核查：源码无真实凭据/违规文案；5 处扩展签名可用且不破坏契约。"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from unittest import mock

from bank import QuestionBank
from config import ConfigError
from executor import TaskExecutor
from fixtures import RESULT_HTML, config_copy
from models import AppConfig, Task
from platform_client import Paper
from state_store import StateStore
from webapp import WebApp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_FILES = [
    "main.py", "config.py", "models.py", "utils.py", "logger.py", "platform_client.py",
    "parsers.py", "planner.py", "answer_engine.py", "executor.py", "state_store.py",
    "bank.py", "exporters.py", "webapp.py",
    "static/index.html", "static/app.css", "static/app.js", "config.example.json",
]


class FakeClient:
    def __init__(self):
        self.generate_calls = []

    def get_result_html(self, session_id):
        return RESULT_HTML

    def generate_paper(self, node_id, md_path, paper=None):
        self.generate_calls.append((node_id, md_path))
        return Paper(session_id="sess-1234567890abcdef")


class StepClock:
    def __call__(self):
        return 0.0


class TestSourceCompliance(unittest.TestCase):
    def _read_sources(self):
        text = {}
        for relative in SOURCE_FILES:
            path = os.path.join(ROOT, relative)
            with open(path, "r", encoding="utf-8") as handle:
                text[relative] = handle.read()
        return text

    def test_no_banned_review_wording_in_source_or_ui(self) -> None:
        # 违规措辞仅在需求文档中作为"禁止项"出现，源码/界面不得出现
        banned = ["刷分", "刷壳", "满分达成", "重刷"]
        for name, text in self._read_sources().items():
            for word in banned:
                self.assertNotIn(word, text, f"{name} 含违规文案：{word}")

    def test_no_real_credentials_or_internal_ip_in_source(self) -> None:
        # 站点地址属于公开信息，允许预置在配置模板 config.example.json 中；
        # 但任何源码/界面/模板都不得出现真实账号密码或凭据赋值。
        ip_patterns = [
            re.compile(r"192\.168\.\d+\.\d+"),
            re.compile(r"10\.\d+\.\d+\.\d+"),
        ]
        credential_patterns = [
            re.compile(r"(?i)password\s*[:=]\s*['\"][A-Za-z0-9!@#$%^&*]{6,}['\"]"),
        ]
        for name, text in self._read_sources().items():
            for pattern in credential_patterns:
                self.assertIsNone(pattern.search(text), f"{name} 疑似残留真实凭据：{pattern.pattern}")
            if name == "config.example.json":
                continue  # 该文件允许预置公开站点地址（base_url）
            for pattern in ip_patterns:
                self.assertIsNone(pattern.search(text), f"{name} 疑似硬编码内网地址：{pattern.pattern}")

    def test_config_template_has_site_url_but_no_credentials(self) -> None:
        # 模板可预置公开课程站点地址以便同学零配置使用，但必须：地址合法、无账号、无密码。
        with open(os.path.join(ROOT, "config.example.json"), "r", encoding="utf-8") as handle:
            data = json.load(handle)
            handle.seek(0)
            text = handle.read()
        self.assertTrue(str(data.get("base_url", "")).startswith(("http://", "https://")))
        self.assertEqual(str(data.get("username", "")), "")  # 学号留空，由网页填写
        self.assertNotIn("password\"", text.replace("password_env", ""))  # 不含密码字段
        # 学号是 10 位数字；用正则判断，避免把真实学号写进本文件（仓库会公开）
        self.assertIsNone(re.search(r"\d{10}", text), "模板中残留了疑似学号的 10 位数字")

    def test_gitignore_covers_runtime_artifacts(self) -> None:
        with open(os.path.join(ROOT, ".gitignore"), "r", encoding="utf-8") as handle:
            text = handle.read()
        for name in ("config.json", "data/", "logs/", "exports/"):
            self.assertIn(name, text)


class TestDocumentedDeviations(unittest.TestCase):
    """工程师主动披露的签名扩展，验证未破坏既有契约。"""

    def test_app_config_load_extra_flags(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.json")
            AppConfig._from_dict(config_copy()).save(path)
            env = {k: v for k, v in os.environ.items() if not k.startswith("CQA_")}
            env["CQA_BASE_URL"] = "http://env-only.invalid/"
            with mock.patch.dict(os.environ, env, clear=True):
                cfg = AppConfig.load(path, require_password=False, apply_env=False)
                self.assertEqual(cfg.base_url, "http://example.invalid:5000/")
                cfg_env = AppConfig.load(path, require_password=False, apply_env=True)
                self.assertEqual(cfg_env.base_url, "http://env-only.invalid/")

    def test_task_from_node_and_path_chapter_hint(self) -> None:
        task = Task.from_node_and_path({}, "裸文件名.md", chapter_hint="自主学习")
        self.assertEqual(task.chapter, "自主学习")
        self.assertEqual(task.unique_key, "::裸文件名.md")

    def test_wait_and_collect_optional_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = AppConfig._from_dict(config_copy())
            executor = TaskExecutor(FakeClient(), None, StateStore(os.path.join(folder, "state.json")),
                                    QuestionBank(), None, config, sleep_fn=lambda _: None, clock=StepClock())
            task = Task(unique_key="n::a.md", chapter="2_测序")
            score, html = executor.wait_and_collect("sess-1234567890abcdef", task)
            self.assertEqual(score, 90.0)
            self.assertIn("examData", html)
            score2, _ = executor.wait_and_collect("sess-1234567890abcdef", task, deadline=999.0)
            self.assertEqual(score2, 90.0)

    def test_webapp_api_export_returns_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = AppConfig._from_dict(config_copy())
            app = WebApp(config, StateStore(os.path.join(folder, "state.json")),
                         QuestionBank(), None, None)
            self.assertIsInstance(app.api_export("json"), bytes)

    def test_planner_skip_decision_lives_in_executor(self) -> None:
        # 契约核查：build_plan 只排序/限量，不产出 SKIPPED 状态
        from planner import build_plan
        from fixtures import SMALL_TREE
        done_key = "node_A::2_测序/基因组测序.md"
        plan = build_plan(SMALL_TREE["tree"], [], 4, [done_key])
        self.assertEqual(len(plan), 4)
        self.assertTrue(all(hasattr(task, "unique_key") for task in plan))
        self.assertFalse(any(hasattr(task, "status") for task in plan))


if __name__ == "__main__":
    unittest.main()
