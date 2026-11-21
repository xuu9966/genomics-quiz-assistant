"""planner.py 用例：章节筛选、限量顺序、完成键排序与 force。"""
from __future__ import annotations

import unittest

import parsers
from fixtures import SMALL_TREE, make_tree
from planner import Planner, build_plan, parse_limit_argument


class TestBuildPlan(unittest.TestCase):
    def test_full_plan_contains_chapter_name_and_unique_key(self) -> None:
        plan = build_plan(SMALL_TREE["tree"], [], 0, [])
        self.assertEqual(len(plan), 4)
        self.assertEqual(plan[0].unique_key, "node_A::2_测序/基因组测序.md")
        self.assertEqual(plan[0].chapter, "2_测序")
        self.assertEqual(plan[0].file_name, "基因组测序.md")
        self.assertEqual(plan[2].chapter, "3_组装")

    def test_chapter_filter_by_number(self) -> None:
        plan = build_plan(SMALL_TREE["tree"], ["2"], 0, [])
        self.assertEqual(len(plan), 2)
        self.assertTrue(all(task.chapter == "2_测序" for task in plan))

    def test_chapter_filter_by_name_prefix(self) -> None:
        plan = build_plan(SMALL_TREE["tree"], ["自主学习"], 0, [])
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].node_id, "node_C1")

    def test_filter_then_limit(self) -> None:
        tree = make_tree(20)["tree"]
        plan = build_plan(tree, ["2"], 3, [])
        self.assertEqual(len(plan), 3)

    def test_completed_keys_sorted_behind_pending(self) -> None:
        plan_all = build_plan(SMALL_TREE["tree"], [], 0, [])
        first = plan_all[0].unique_key
        second = plan_all[1].unique_key
        plan = build_plan(SMALL_TREE["tree"], [], 2, [first, second])
        self.assertEqual([t.unique_key for t in plan], [plan_all[2].unique_key, plan_all[3].unique_key])

    def test_force_ignores_completed_keys(self) -> None:
        plan_all = build_plan(SMALL_TREE["tree"], [], 0, [])
        first = plan_all[0].unique_key
        plan = build_plan(SMALL_TREE["tree"], [], 2, [first], force=True)
        self.assertEqual(plan[0].unique_key, first)

    def test_plan_keeps_completed_items_when_limit_allows(self) -> None:
        plan_all = build_plan(SMALL_TREE["tree"], [], 0, [])
        plan = build_plan(SMALL_TREE["tree"], [], 4, [plan_all[0].unique_key])
        self.assertEqual(len(plan), 4)
        self.assertEqual(plan[-1].unique_key, plan_all[0].unique_key)

    def test_zero_tasks_raises_parse_error(self) -> None:
        with self.assertRaises(parsers.ParseError) as ctx:
            build_plan({"id": "root", "name": "空目录"}, [], 10, [])
        self.assertIn("页面结构可能变化", str(ctx.exception))

    def test_unmatched_chapter_raises_parse_error(self) -> None:
        with self.assertRaises(parsers.ParseError):
            build_plan(SMALL_TREE["tree"], ["不存在的章节"], 10, [])

    def test_facade_signature(self) -> None:
        self.assertEqual(len(Planner.build_plan(SMALL_TREE["tree"], [], 0, [])), 4)
        self.assertTrue(Planner.match_chapter("2_测序/a.md", {"name": "节点"}, ["2"]))
        self.assertEqual(Planner.normalize_md_path("a\\b.md"), "a/b.md")


class TestLimitArgument(unittest.TestCase):
    def test_default_and_positive(self) -> None:
        self.assertEqual(parse_limit_argument(""), 10)
        self.assertEqual(parse_limit_argument("5"), 5)

    def test_all_requires_confirmation(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_limit_argument("ALL")
        self.assertIn("二次确认", str(ctx.exception))
        self.assertEqual(parse_limit_argument("ALL", True), 0)

    def test_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            parse_limit_argument("abc")
        with self.assertRaises(ValueError):
            parse_limit_argument("-1")


if __name__ == "__main__":
    unittest.main()
