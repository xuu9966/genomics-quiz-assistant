"""webapp.py 用例：/api/state 完整计划+记录+日志、筛选导出、回环绑定。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from bank import QuestionBank
from fixtures import config_copy
from models import AppConfig, ExecutionRecord, RunState, Task
from state_store import StateStore
from webapp import WebApp, export_filename, export_mime


class FakeLogger:
    def __init__(self):
        self.lines = ["[时间] [INFO] 复习完成：90 分"]

    def tail(self, n=50):
        return self.lines[-n:]

    def info(self, message, task_key=""):
        self.lines.append(message)

    def error(self, message, task_key=""):
        self.lines.append(message)


class FakeExecutor:
    def __init__(self):
        self.queued = []

    def queue_rerun(self, keys):
        self.queued.extend(keys)
        return len(self.queued)


class TestWebAppApis(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = AppConfig._from_dict(config_copy())
        self.config.password = "虚构密码-DO-NOT-EXPORT"
        self.store = StateStore(os.path.join(self.tmp.name, "state.json"))
        self.bank = QuestionBank()
        self.logger = FakeLogger()
        self.executor = FakeExecutor()
        self.t1 = Task(unique_key="n1::2_测序/a.md", node_id="n1", md_path="2_测序/a.md",
                       file_name="a.md", chapter="2_测序")
        self.t2 = Task(unique_key="n2::3_组装/b.md", node_id="n2", md_path="3_组装/b.md",
                       file_name="b.md", chapter="3_组装")
        self.store.save(RunState(
            config_masked=self.config.safe_dict(),
            plan=[self.t1, self.t2],
            records={self.t1.unique_key: ExecutionRecord(
                unique_key=self.t1.unique_key, status="success", score=92.0,
                session_id="sess-1234567890abcdef")},
        ))
        self.bank.add(__import__("models").Question(
            content="测序考点", analysis="解析", options=["甲", "乙"],
            correct_answer="甲", sources=["AI辅助自测"], chapter="2_测序"))
        self.app = WebApp(self.config, self.store, self.bank, self.logger, self.executor)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_api_state_returns_full_plan_records_logs_and_masked_config(self) -> None:
        payload = self.app.api_state()
        self.assertEqual(len(payload["plan"]), 2)
        self.assertEqual(set(payload["records"].keys()), {self.t1.unique_key})
        self.assertEqual(payload["records"][self.t1.unique_key]["score"], 92.0)
        self.assertEqual(payload["logs"], self.logger.lines)
        self.assertEqual(payload["poll_sec"], 3)
        self.assertFalse(payload["status_color"])
        self.assertEqual(payload["summary"]["total"], 2)
        self.assertEqual(payload["summary"]["done"], 1)
        self.assertEqual(payload["config_masked"]["password"], "***")
        self.assertNotIn("虚构密码-DO-NOT-EXPORT", json.dumps(payload, ensure_ascii=False))

    def test_api_state_reflects_record_change_without_restart(self) -> None:
        before = self.app.api_state()["summary"]["done"]
        self.store.upsert(ExecutionRecord(unique_key=self.t2.unique_key, status="success", score=80.0))
        after = self.app.api_state()["summary"]["done"]
        self.assertEqual((before, after), (1, 2))

    def test_api_records_search_and_status_filter(self) -> None:
        all_rows = self.app.api_records({})
        self.assertEqual(all_rows["total"], 2)
        self.assertEqual(self.app.api_records({"status": "success"})["total"], 1)
        self.assertEqual(self.app.api_records({"status": "pending"})["total"], 1)
        self.assertEqual(self.app.api_records({"chapter": "3_组装"})["total"], 1)
        self.assertEqual(self.app.api_records({"keyword": "A.MD"})["total"], 1)

    def test_api_questions_filter(self) -> None:
        self.assertEqual(self.app.api_questions({})["total"], 1)
        self.assertEqual(self.app.api_questions({"keyword": "测序"})["total"], 1)
        self.assertEqual(self.app.api_questions({"keyword": "不存在"})["total"], 0)
        self.assertEqual(self.app.api_questions({"chapter": "3_组装"})["total"], 0)

    def test_api_export_all_formats_are_bytes_and_credential_free(self) -> None:
        for fmt in ("html", "json", "md", "csv"):
            with self.subTest(fmt=fmt):
                payload = self.app.api_export(fmt)
                self.assertIsInstance(payload, bytes)
                self.assertNotIn(b"s3cr3t", payload)
                self.assertNotIn("虚构密码-DO-NOT-EXPORT".encode("utf-8"), payload)

    def test_api_export_csv_contains_plan_rows(self) -> None:
        text = self.app.api_export("csv").decode("utf-8-sig")
        self.assertIn("n1::2_测序/a.md", text)
        self.assertIn("status", text)

    def test_api_control_clear_and_queue(self) -> None:
        result = self.app.api_control("force_rerun", [self.t1.unique_key])
        self.assertTrue(result["ok"])
        self.assertEqual(result["queued"], 1)
        self.assertIsNone(self.store.get(self.t1.unique_key))
        self.assertEqual(self.executor.queued, [self.t1.unique_key])

    def test_api_control_unknown_action(self) -> None:
        self.assertFalse(self.app.api_control("unknown", [])["ok"])

    def test_export_helpers(self) -> None:
        self.assertEqual(export_filename("csv"), "quiz-records.csv")
        self.assertEqual(export_filename("json"), "quiz-bank.json")
        self.assertTrue(export_mime("json").startswith("application/json"))


class TestLoopbackBinding(unittest.TestCase):
    def test_server_binds_loopback_only(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = AppConfig._from_dict(config_copy())
            config.web["port"] = 0
            store = StateStore(os.path.join(folder, "state.json"))
            app = WebApp(config, store, QuestionBank(), None, None)
            server = None
            try:
                server = app.start(block=False)
                self.assertEqual(server.server_address[0], "127.0.0.1")
            finally:
                # 未调用 serve_forever 时 shutdown() 会一直等待，这里只关闭监听套接字
                if server is not None:
                    server.server_close()


if __name__ == "__main__":
    unittest.main()
