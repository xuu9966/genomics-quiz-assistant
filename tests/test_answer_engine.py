"""answer_engine.py 用例：单选/多选解析、无法解析保留原始返回。"""
from __future__ import annotations

import unittest

from answer_engine import AnswerParseError, PlatformChatAnswerEngine, selection_to_texts
from models import QType

SINGLE = {"id": "q1", "qtype": QType.SINGLE.value, "content": "单选题？",
          "options": ["A文本", "B文本", "C文本", "D文本"]}
MULTIPLE = {"id": "q2", "qtype": QType.MULTIPLE.value, "content": "多选题？",
            "options": ["选项一", "选项二", "选项三", "选项四"]}


class TestParseOption(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PlatformChatAnswerEngine()

    def test_single_letters(self) -> None:
        for raw, expected in (("B", "B"), ("答案：C。", "C"), ("正确答案是 D", "D"),
                              ("A. A文本", "A")):
            with self.subTest(raw=raw):
                self.assertEqual(self.engine.parse_option(raw, SINGLE), expected)

    def test_multiple_letters(self) -> None:
        self.assertEqual(self.engine.parse_option("A,C", MULTIPLE), "A,C")
        self.assertEqual(self.engine.parse_option("答案：B,D", MULTIPLE), "B,D")

    def test_numeric_answer(self) -> None:
        self.assertEqual(self.engine.parse_option("2", SINGLE), "B")
        self.assertEqual(self.engine.parse_option("1,3", MULTIPLE), "A,C")

    def test_option_text_answer(self) -> None:
        self.assertEqual(self.engine.parse_option("B文本", SINGLE), "B")
        self.assertEqual(self.engine.parse_option("选项二", MULTIPLE), "B")

    def test_empty_or_unparseable_raises(self) -> None:
        for raw in ("", "无法判断", "我不知道选哪个"):
            with self.subTest(raw=raw), self.assertRaises(AnswerParseError):
                self.engine.parse_option(raw, SINGLE)

    def test_missing_options_raises(self) -> None:
        with self.assertRaises(AnswerParseError):
            self.engine.parse_option("A", {"content": "无选项题", "options": []})

    def test_selection_to_texts(self) -> None:
        self.assertEqual(selection_to_texts(MULTIPLE, "A,C"), ["选项一", "选项三"])
        self.assertEqual(selection_to_texts(SINGLE, "B"), ["B文本"])


class TestAnswerDetail(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PlatformChatAnswerEngine()

    def test_success_keeps_selected_text(self) -> None:
        detail = self.engine.answer(MULTIPLE, lambda _: "A,C")
        self.assertTrue(detail.ok)
        self.assertEqual(detail.final_selection, "A,C")
        self.assertEqual(detail.selected_text, "选项一；选项三")
        self.assertIn("A,C", detail.raw_response)

    def test_parse_failure_marks_not_ok_and_keeps_raw(self) -> None:
        detail = self.engine.answer(SINGLE, lambda _: "无法判断")
        self.assertFalse(detail.ok)
        self.assertEqual(detail.final_selection, "")
        self.assertIn("无法判断", detail.raw_response)
        self.assertIn("[解析失败]", detail.raw_response)

    def test_chat_exception_is_captured(self) -> None:
        def boom(_):
            raise RuntimeError("问答服务不可用")

        detail = self.engine.answer(SINGLE, boom)
        self.assertFalse(detail.ok)
        self.assertIn("问答服务不可用", detail.raw_response)

    def test_prompt_is_built_with_lettered_options(self) -> None:
        prompt = self.engine.build_prompt(SINGLE)
        self.assertIn("A. A文本", prompt)
        self.assertIn("单选题？", prompt)


if __name__ == "__main__":
    unittest.main()
