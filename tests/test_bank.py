"""bank.py 用例：双来源采集、MD5 去重、多来源合并、检索、图片题标记。"""
from __future__ import annotations

import json
import unittest

from bank import QuestionBank
from fixtures import CONCLUSION_JSON, RESULT_HTML
from models import QType, Question


class TestQuestionBank(unittest.TestCase):
    def test_collect_from_result_and_dedup_by_question_text(self) -> None:
        bank = QuestionBank()
        added = bank.collect_from_result(RESULT_HTML, "2_测序")
        self.assertEqual(added, 2)
        self.assertEqual(len(bank), 2)
        # 重复采集同页不应新增题目（题干 MD5 去重）
        self.assertEqual(bank.collect_from_result(RESULT_HTML, "2_测序"), 0)
        self.assertEqual(len(bank), 2)

    def test_image_question_flagged(self) -> None:
        bank = QuestionBank()
        bank.collect_from_result(RESULT_HTML, "2_测序")
        image_questions = [q for q in bank.all() if q.has_image]
        self.assertEqual(len(image_questions), 1)
        self.assertIn("img", image_questions[0].content.lower())

    def test_flag_one_option_becomes_correct_answer(self) -> None:
        bank = QuestionBank()
        bank.collect_from_result(RESULT_HTML, "2_测序")
        q1 = [q for q in bank.all() if q.qid == "q1"][0]
        self.assertEqual(q1.correct_answer, "Sanger测序")
        q2 = [q for q in bank.all() if q.qid == "q2"][0]
        self.assertEqual(q1.qtype, QType.SINGLE.value)
        self.assertEqual(q2.qtype, QType.SINGLE.value)  # examData 未给题型时默认单选

    def test_dual_source_merge_keeps_both_analysis_and_sources(self) -> None:
        bank = QuestionBank()
        html = (
            "window.examData = [{\"question_id\":\"q1\",\"content\":\"下列哪一项属于DNA测序技术？\","
            "\"options\":[\"Sanger测序\",\"PCR扩增\"],\"correct_answer\":\"Sanger测序\","
            "\"analysis\":\"来自自测的解析\"}];"
        )
        bank.collect_from_result(html, "2_测序")
        bank.collect_from_conclusion(CONCLUSION_JSON, "2_测序")
        self.assertEqual(len(bank), 2)  # 同题合并为 1 题 + 结论里的另一题
        merged = [q for q in bank.all() if q.qid == "q1"][0]
        self.assertIn("来自自测的解析", merged.analysis)
        self.assertIn("来自评阅总结的补充解析", merged.analysis)
        self.assertIn("AI辅助自测", merged.sources)
        self.assertIn("AI评阅总结", merged.sources)
        self.assertEqual(merged.correct_answer, "Sanger测序")

    def test_search_by_keyword_type_and_chapter(self) -> None:
        bank = QuestionBank()
        bank.add(Question(content="测序技术考点", analysis="解析甲", correct_answer="A",
                          options=["甲", "乙"], qtype=QType.SINGLE.value, sources=["来源1"], chapter="2_测序"))
        bank.add(Question(content="组装算法考点", analysis="解析乙", correct_answer="B",
                          options=["丙", "丁"], qtype=QType.MULTIPLE.value, sources=["来源2"], chapter="3_组装"))
        self.assertEqual(len(bank.search(keyword="测序")), 1)
        self.assertEqual(len(bank.search(qtype="qt02")), 1)
        self.assertEqual(len(bank.search(chapter="3_组装")), 1)
        self.assertEqual(len(bank.search(keyword="解析乙")), 1)
        self.assertEqual(len(bank.search()), 2)

    def test_m6_all_questions_unique_by_content(self) -> None:
        bank = QuestionBank()
        bank.collect_from_result(RESULT_HTML, "2_测序")
        bank.collect_from_conclusion(CONCLUSION_JSON, "2_测序")
        contents = [q.content for q in bank.all()]
        self.assertEqual(len(contents), len(set(contents)))
        self.assertGreater(len(contents), 0)

    def test_empty_content_ignored(self) -> None:
        bank = QuestionBank()
        bank.add(Question(content=""))
        self.assertEqual(len(bank), 0)

    def test_to_dicts_serializable(self) -> None:
        bank = QuestionBank()
        bank.collect_from_result(RESULT_HTML)
        self.assertIsInstance(json.dumps(bank.to_dicts(), ensure_ascii=False), str)


if __name__ == "__main__":
    unittest.main()
