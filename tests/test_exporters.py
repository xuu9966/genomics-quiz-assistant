"""exporters.py 用例：HTML/MD/JSON/CSV 无凭据、图片保留与标记、字段完整。"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest

import exporters
from models import ExecutionRecord, Question

SECRET = "s3cr3t-DO-NOT-EXPORT"


def _question() -> Question:
    return Question(
        qid="q1", qtype="qt02", content="题面含图片 <img src='https://example.invalid/q.png'>，注意 password=" + SECRET,
        options=["选项A", "选项B"], correct_answer="A",
        analysis="解析内容 password=" + SECRET,
        sources=["AI辅助自测", "AI评阅总结"], has_image=True, chapter="2_测序",
    )


class TestBankExports(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_html_keeps_safe_image_and_masks_credentials(self) -> None:
        path = self._path("bank.html")
        exporters.bank_html([_question()], path)
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertLessEqual(text.count(SECRET), 0)
        self.assertIn('<img src="https://example.invalid/q.png"', text)
        self.assertIn("[含图片，请查看原始题面]", text)
        # 回归：CSS 应为合法的 100%，不得残留模板转义符 %%
        self.assertIn("max-width:100%", text)
        self.assertNotIn("100%%", text)
        self.assertIn("data-keyword=", text)
        self.assertIn("<details", text)
        self.assertIn("@media print", text)
        self.assertIn("user-select:text", text)

    def test_html_rejects_unsafe_image_source(self) -> None:
        question = Question(content="图片题 <img src='file:///C:/secret/local.png'>", has_image=True)
        path = self._path("unsafe.html")
        exporters.bank_html([question], path)
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        # 题面原文中的非法地址仍以转义文本保留（不可点击），但不得生成可加载的 img 标签
        self.assertNotIn('<img src="file:///', text)

    def test_markdown_marks_image_question_and_masks(self) -> None:
        path = self._path("bank.md")
        exporters.bank_markdown([_question()], path)
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("> [含图片，请查看原始题面]", text)
        self.assertIn("多选题", text)
        self.assertNotIn(SECRET, text)

    def test_json_keeps_complete_fields_and_masks(self) -> None:
        path = self._path("bank.json")
        exporters.bank_json([_question()], path)
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(len(data), 1)
        for field in ("dedup_key", "qid", "qtype", "content", "options",
                      "correct_answer", "analysis", "sources", "has_image", "chapter"):
            self.assertIn(field, data[0])
        self.assertNotIn(SECRET, json.dumps(data, ensure_ascii=False))

    def test_records_csv_has_no_password_field_or_secret(self) -> None:
        path = self._path("records.csv")
        record = ExecutionRecord(unique_key="node_A::2_测序/a.md", status="success",
                                 score=90, session_id="sess-1234567890abcdef")
        exporters.records_csv([dict(record.to_dict(), password=SECRET,
                                    file_name="a.md", node_id="node_A")], path)
        with open(path, "r", encoding="utf-8", newline="") as handle:
            text = handle.read()
        self.assertNotIn(SECRET, text)
        self.assertNotIn("password", text)
        self.assertIn("node_A::2_测序/a.md", text)

    def test_bank_bytes_returns_all_formats(self) -> None:
        for fmt in ("html", "json", "md", "markdown"):
            with self.subTest(fmt=fmt):
                payload = exporters.bank_bytes(fmt, [_question()])
                self.assertIsInstance(payload, bytes)
                self.assertGreater(len(payload), 0)
                self.assertNotIn(SECRET.encode("utf-8"), payload)

    def test_text_stream_target_supported(self) -> None:
        buffer = io.StringIO()
        exporters.bank_markdown([_question()], buffer)
        self.assertIn("复习题库", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
