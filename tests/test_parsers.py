"""parsers.py 纯解析用例：目录树、窗口变量、分数、examData、conclusion。"""
from __future__ import annotations

import json
import unittest

import parsers

from fixtures import (
    CONCLUSION_JSON,
    RESULT_HTML,
    RESULT_HTML_NO_SCORE,
    SMALL_GRAPH_HTML,
    SMALL_TREE,
    make_tree,
)


class TestParseGraphData(unittest.TestCase):
    def test_parse_dict_tree(self) -> None:
        data = parsers.parse_graph_data(SMALL_GRAPH_HTML)
        self.assertEqual(data.get("course"), "虚构课程")
        self.assertIsInstance(data["tree"], dict)
        self.assertEqual(data["tree"]["id"], "root")

    def test_parse_array_tree(self) -> None:
        html = "<html><script>let graphData = " + json.dumps(make_tree(8, as_list=True), ensure_ascii=False) + ";</script></html>"
        data = parsers.parse_graph_data(html)
        self.assertIsInstance(data["tree"], list)

    def test_braces_inside_string_do_not_break_balanced_slice(self) -> None:
        html = r'<script>var graphData={"course":"带}括号的课程","tree":{"id":"x","name":"节点"}};</script>'
        data = parsers.parse_graph_data(html)
        self.assertEqual(data["course"], "带}括号的课程")

    def test_missing_marker_raises_readable_parse_error(self) -> None:
        with self.assertRaises(parsers.ParseError) as ctx:
            parsers.parse_graph_data("<html>没有任何数据</html>")
        self.assertIn("页面结构可能变化", str(ctx.exception))

    def test_malformed_object_raises_parse_error(self) -> None:
        with self.assertRaises(parsers.ParseError) as ctx:
            parsers.parse_graph_data("<script>var graphData = {broken json;</script>")
        self.assertIn("页面结构可能变化", str(ctx.exception))

    def test_missing_tree_field_raises_parse_error(self) -> None:
        with self.assertRaises(parsers.ParseError):
            parsers.parse_graph_data("<script>var graphData = {\"course\":\"x\"};</script>")


class TestTreeTraversal(unittest.TestCase):
    def test_recursive_traversal_796_files_no_loss(self) -> None:
        # Arrange：796 个 md 分散在 4 顶层 × 5 子节点 × 叶子节点中，子节点字段名混用
        tree = make_tree(796)["tree"]
        # Act
        nodes = list(parsers.iter_tree_nodes(tree))
        collected = [path for node in nodes for path in parsers.node_files(node)]
        # Assert
        self.assertEqual(len(collected), 796)
        self.assertEqual(len(set(collected)), 796)
        self.assertTrue(all(path.endswith(".md") for path in collected))

    def test_array_tree_also_traversed(self) -> None:
        tree = make_tree(120, as_list=True)["tree"]
        collected = [p for n in parsers.iter_tree_nodes(tree) for p in parsers.node_files(n)]
        self.assertEqual(len(collected), 120)

    def test_custom_array_field_and_non_md_ignored(self) -> None:
        tree = {"id": "r", "custom_children": [{"id": "a", "files": ["a.md", "notes.txt"]}]}
        collected = [p for n in parsers.iter_tree_nodes(tree) for p in parsers.node_files(n)]
        self.assertEqual(collected, ["a.md"])


class TestWindowVariables(unittest.TestCase):
    def test_extract_string_number_object_and_array(self) -> None:
        html = (
            "var kpId = 'KP-0001';\n"
            "window.attempts = 3;\n"
            "window.selftestInfo = {\"current\": 2, \"total\": 10};\n"
            "window.examData = [{\"content\":\"题目\"}];\n"
        )
        self.assertEqual(parsers.extract_window_var(html, "kpId"), "KP-0001")
        self.assertEqual(parsers.extract_window_var(html, "attempts"), 3)
        self.assertEqual(parsers.extract_window_var(html, "selftestInfo"), {"current": 2, "total": 10})
        self.assertEqual(parsers.extract_window_var(html, "examData"), [{"content": "题目"}])

    def test_missing_variable_returns_none(self) -> None:
        self.assertIsNone(parsers.extract_window_var("<html></html>", "missing"))

    def test_exam_data_list_and_wrapped_object(self) -> None:
        self.assertEqual(len(parsers.extract_exam_data(RESULT_HTML)), 2)
        wrapped = "window.examData = {\"questions\": [{\"content\":\"题干\"}]};"
        self.assertEqual(len(parsers.extract_exam_data(wrapped)), 1)

    def test_exam_data_string_json_and_invalid(self) -> None:
        html = "window.examData = '[{\"content\":\"题干\"}]';"
        self.assertEqual(len(parsers.extract_exam_data(html)), 1)
        self.assertEqual(parsers.extract_exam_data("window.examData = not-json;"), [])


class TestScoreAndConclusion(unittest.TestCase):
    def test_extract_score_forms(self) -> None:
        self.assertEqual(parsers.extract_score(RESULT_HTML), 90.0)
        self.assertEqual(parsers.extract_score("<div>已评阅：85.5 分</div>"), 85.5)
        self.assertIsNone(parsers.extract_score(RESULT_HTML_NO_SCORE))

    def test_parse_conclusion_fields(self) -> None:
        questions = parsers.parse_conclusion(CONCLUSION_JSON, "2_测序")
        self.assertEqual(len(questions), 2)
        first = questions[0]
        self.assertEqual(first.qid, "q1")
        self.assertEqual(first.correct_answer, "Sanger测序")
        self.assertIn("评阅总结", first.analysis)
        self.assertEqual(first.sources, ["AI评阅总结"])
        self.assertEqual(first.chapter, "2_测序")
        self.assertEqual(questions[1].qtype, "qt02")

    def test_parse_conclusion_bad_input_returns_empty(self) -> None:
        self.assertEqual(parsers.parse_conclusion("not json"), [])
        self.assertEqual(parsers.parse_conclusion({"low_rate_questions": "oops"}), [])

    def test_parse_question_types(self) -> None:
        result = parsers.parse_question_types({"data": [{"id": "qt01", "name": "单选题"}]})
        self.assertEqual(result, {"qt01": "单选题"})


if __name__ == "__main__":
    unittest.main()
