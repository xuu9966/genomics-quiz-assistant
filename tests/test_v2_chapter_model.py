"""v2 大章 × 第k次模型用例（N1~N10，全程假 transport / 假 chat，绝不触网）。

对应用户拍板后的认知纠正：任务单元=顶层大章节点；唯一键=大章id::第k次；
同章所有 k 共享大章 node_id；完成判定只看本地 SUCCESS；断点续跑按 k 补齐。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

import config
import parsers
from answer_engine import PlatformChatAnswerEngine
from bank import QuestionBank
from config import ConfigError
from executor import TaskExecutor
from fixtures import (
    CHAPTER_IDS,
    CHAPTER_KP_IDS,
    CHAPTER_NAMES,
    RESULT_HTML,
    config_copy,
    make_chapter_tree,
)
from main import _chapter_outline
from models import AppConfig, ExecutionRecord, RunState, TaskStatus
from planner import Planner, build_chapter_plan
from platform_client import Paper
from state_store import StateStore
from utils import make_task_key


class FakeChapterClient:
    """平台适配层替身：只记录调用；组卷回传的 node_id 就是传入的大章 id。"""

    def __init__(self) -> None:
        self.generate_calls: list = []
        self.submit_calls: list = []
        self.chat_calls: list = []
        self.result_calls: int = 0
        self.session_index: int = 0
        self.chat_responses: list = ["B", "A,C"]

    def generate_paper(self, node_id: str, md_path: str, paper=None) -> Paper:
        self.generate_calls.append((str(node_id), str(md_path)))
        self.session_index += 1
        return Paper(
            session_id=f"sess-v2-{self.session_index:04d}",
            redirect="",
            md_name=os.path.basename(str(md_path).replace("\\", "/")),
            node_id=str(node_id),
            task_name="大章自测",
            duration=0,
            rules=["先自测，后看解析"],
            questions={
                "qt01": [{"id": "q1", "content": "单选题干", "options": ["甲", "乙", "丙", "丁"]}],
                "qt02": [{"id": "q2", "content": "多选题干", "options": ["A项", "B项", "C项", "D项"]}],
            },
        )

    def chat(self, message: str, model=None) -> str:
        self.chat_calls.append((message, model))
        index = len(self.chat_calls) - 1
        return self.chat_responses[index % len(self.chat_responses)]

    def submit_paper(self, session_id: str, payload: dict) -> None:
        self.submit_calls.append((session_id, dict(payload)))

    def get_result_html(self, session_id: str) -> str:
        self.result_calls += 1
        return RESULT_HTML

    def list_tasks(self, page: int = 1) -> dict:
        return {"tasks": [], "total_pages": 1}


class FakeLogger:
    def __init__(self) -> None:
        self.entries: list = []

    def log(self, level: str, message: str, task_key: str = "") -> None:
        self.entries.append((level, message, task_key))

    def info(self, message: str, task_key: str = "") -> None:
        self.entries.append(("INFO", message, task_key))


class ChapterTestBase(unittest.TestCase):
    """统一夹具：8 大章树 + 临时状态库 + 可注入执行器。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tree = make_chapter_tree()["tree"]
        self.store = StateStore(os.path.join(self.tmp.name, "state.json"))
        self.config = AppConfig._from_dict(config_copy())
        self.bank = QuestionBank()
        self.logger = FakeLogger()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_executor(self, client: FakeChapterClient) -> TaskExecutor:
        return TaskExecutor(
            client=client, engine=PlatformChatAnswerEngine(), store=self.store,
            bank=self.bank, logger=self.logger, config=self.config,
            sleep_fn=lambda _: None, clock=lambda: 0.0,
        )


class TestChapterModel(ChapterTestBase):
    def test_n1_each_chapter_repeats_default_times_with_chapter_node_id(self) -> None:
        """N1：repeat_default=3 单选 2_测序 → 3 单元，组卷均用大章 id 与代表文件。"""
        plan = build_chapter_plan(self.tree, ["2_测序"], 0, [], repeat_default=3)
        node_id = CHAPTER_IDS[2]
        representative = "2_测序\\代表文件.md"
        self.assertEqual(len(plan), 3)
        self.assertTrue(all(task.node_id == node_id for task in plan))
        self.assertTrue(all(task.md_path == representative for task in plan))

        client = FakeChapterClient()
        records = list(self.make_executor(client).run_plan(plan))
        self.assertEqual([record.status for record in records], [TaskStatus.SUCCESS.value] * 3)
        self.assertEqual(len(client.generate_calls), 3)
        self.assertEqual([call[0] for call in client.generate_calls], [node_id] * 3)
        self.assertTrue(all(call[1] == representative for call in client.generate_calls))
        # 交卷 node_id 用组卷回传值（等于大章 id）
        self.assertTrue(all(payload["node_id"] == node_id for _, payload in client.submit_calls))

    def test_n2_override_counts_match_plan_and_outline(self) -> None:
        """N2：default=3 + {2_测序:5, 4_注释:2} + 三章 → 5/3/2，总量 10。"""
        plan = build_chapter_plan(
            self.tree, ["2_测序", "3_组装", "4_注释"], 0, [],
            repeat_default=3, repeat_overrides={"2_测序": 5, "4_注释": 2},
        )
        counts: dict = {}
        sources: dict = {}
        for task in plan:
            counts[task.chapter] = counts.get(task.chapter, 0) + 1
            sources[task.chapter] = task.repeat_source
        self.assertEqual(counts, {"2_测序": 5, "3_组装": 3, "4_注释": 2})
        self.assertEqual(len(plan), 10)
        self.assertEqual(sources, {
            "2_测序": "override", "3_组装": "default", "4_注释": "override",
        })
        lines = _chapter_outline(plan)
        self.assertTrue(any("2_测序" in line and "5 次" in line and "覆盖" in line for line in lines))
        self.assertTrue(any("3_组装" in line and "3 次" in line and "默认" in line for line in lines))

    def test_n3_task_key_format_is_chapter_id_and_k(self) -> None:
        """N3：唯一键=大章id::k，不再含文件路径。"""
        self.assertEqual(make_task_key(CHAPTER_IDS[2], 3), "20260907180845_0005::3")
        plan = build_chapter_plan(self.tree, ["2_测序"], 0, [], repeat_default=3)
        self.assertEqual([task.unique_key for task in plan], [
            f"{CHAPTER_IDS[2]}::1", f"{CHAPTER_IDS[2]}::2", f"{CHAPTER_IDS[2]}::3",
        ])
        self.assertFalse(any(".md" in task.unique_key or "::2_测序" in task.unique_key
                             for task in plan))

    def test_n4_same_chapter_k_share_node_id_and_kp_id_not_leaf_id(self) -> None:
        """N4：同章不同 k 使用同一 node_id/kpId，绝不使用小文件/子文件夹 id。"""
        plan = build_chapter_plan(self.tree, ["2_测序"], 0, [], repeat_default=4)
        node_ids = {task.node_id for task in plan}
        self.assertEqual(node_ids, {CHAPTER_IDS[2]})
        self.assertEqual({CHAPTER_KP_IDS[task.node_id] for task in plan}, {CHAPTER_IDS[2]})
        leaf_ids = {
            str(node.get("id") or "") for node in parsers.iter_tree_nodes(self.tree)
            if str(node.get("id") or "").endswith("_sub")
        }
        self.assertTrue(leaf_ids)
        self.assertFalse(node_ids & leaf_ids)

    def test_n5_different_chapters_have_independent_node_ids_and_keys(self) -> None:
        """N5：不同大章 node_id 不同、唯一键互不串扰。"""
        plan = build_chapter_plan(self.tree, ["2_测序", "3_组装"], 0, [], repeat_default=2)
        by_chapter: dict = {}
        for task in plan:
            by_chapter.setdefault(task.chapter, []).append(task)
        self.assertEqual(set(by_chapter), {"2_测序", "3_组装"})
        self.assertEqual(len(plan), 4)
        id_2 = by_chapter["2_测序"][0].node_id
        id_3 = by_chapter["3_组装"][0].node_id
        self.assertEqual((id_2, id_3), (CHAPTER_IDS[2], CHAPTER_IDS[3]))
        self.assertNotEqual(id_2, id_3)
        self.assertFalse(
            {task.unique_key for task in by_chapter["2_测序"]} &
            {task.unique_key for task in by_chapter["3_组装"]}
        )

    def test_n6_resume_only_runs_remaining_k_with_zero_generate_for_done(self) -> None:
        """N6：3_组装 5 次已完成 2 次，续跑只补 ::3..::5，前两次组卷调用数=0。"""
        node_id = CHAPTER_IDS[3]
        plan = build_chapter_plan(
            self.tree, ["3_组装"], 0, [],
            repeat_default=5, repeat_overrides={"3_组装": 5},
        )
        self.assertEqual(len(plan), 5)
        for k in (1, 2):
            self.store.upsert(ExecutionRecord(
                unique_key=make_task_key(node_id, k), status=TaskStatus.SUCCESS.value,
                score=88.0, session_id=f"sess-done-{k}",
            ))

        client = FakeChapterClient()
        records = list(self.make_executor(client).run_plan(plan))
        self.assertEqual([record.status for record in records[:2]],
                         [TaskStatus.SKIPPED.value] * 2)
        self.assertEqual([record.status for record in records[2:]],
                         [TaskStatus.SUCCESS.value] * 3)
        self.assertEqual([record.unique_key for record in records[2:]],
                         [f"{node_id}::3", f"{node_id}::4", f"{node_id}::5"])
        self.assertEqual(len(client.generate_calls), 3)
        self.assertEqual([call[0] for call in client.generate_calls], [node_id] * 3)

    def test_n7_authoritative_chapter_list_and_outline_representation(self) -> None:
        """N7：8 章权威清单含 9_复习、缺 7/8；展示含章名/id/M/来源（章名与节点 id 均可覆盖）。"""
        chapters = parsers.top_chapter_nodes(self.tree)
        names = [node["name"] for node in chapters]
        self.assertEqual(names, CHAPTER_NAMES)
        self.assertEqual([node["id"] for node in chapters], CHAPTER_IDS)
        self.assertIn("9_复习", names)
        self.assertFalse(any(name.startswith(("7_", "8_")) for name in names))

        plan = build_chapter_plan(
            self.tree, [], 0, [], repeat_default=3,
            repeat_overrides={CHAPTER_IDS[0]: 2, "9_复习": 4},
        )
        lines = _chapter_outline(plan)
        self.assertEqual(len(lines), 8)
        self.assertTrue(any("0_预习" in line and "0001" in line and "2 次" in line
                            and "覆盖" in line for line in lines))
        self.assertTrue(any("9_复习" in line and "4 次" in line and "覆盖" in line for line in lines))
        self.assertTrue(any("2_测序" in line and "3 次" in line and "默认" in line for line in lines))
        self.assertTrue(any(task.label == "2_测序 第1次" for task in plan))
        self.assertTrue(any(task.label == "9_复习 第4次" for task in plan))
        self.assertEqual(len(Planner.build_chapter_plan(self.tree, [], 0, [])), 24)

    def test_n8_invalid_repeat_values_raise_readable_config_error(self) -> None:
        """N8：0/负数/bool/字符串/超上限均给 ConfigError；旧配置无新字段仍合法。"""
        for value in (0, -1, True, "3", 100):
            with self.subTest(repeat_default=value):
                data = config_copy()
                data["repeat_default"] = value
                with self.assertRaises(ConfigError) as ctx:
                    config.validate_config(data, require_password=False)
                self.assertIn("repeat_default", str(ctx.exception))

        for value in ({"2_测序": 0}, {"2_测序": -2}, {"2_测序": True},
                      {"2_测序": "5"}, {"2_测序": 100}, {"": 3}):
            with self.subTest(repeat_overrides=value):
                data = config_copy()
                data["repeat_overrides"] = value
                with self.assertRaises(ConfigError) as ctx:
                    config.validate_config(data, require_password=False)
                self.assertIn("repeat_overrides", str(ctx.exception))

        legacy = config_copy()
        legacy.pop("repeat_default", None)
        legacy.pop("repeat_overrides", None)
        config.validate_config(legacy, require_password=False)
        self.assertEqual(AppConfig._from_dict(legacy).repeat_default, 3)
        self.assertEqual(AppConfig._from_dict(legacy).repeat_overrides, {})

    def test_n9_v1_state_is_archived_and_not_migrated(self) -> None:
        """N9：v1 state 加载即归档隔离，completed_keys 为空，新写入含 schema_version=2。"""
        path = os.path.join(self.tmp.name, "data", "state.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        legacy_record = {
            "unique_key": "node_A::2_测序/a.md", "status": "success", "score": 90.0,
        }
        legacy_state = {
            "config_masked": {}, "updated_at": "v1-time",
            "plan": [{"unique_key": "node_A::2_测序/a.md", "node_id": "node_A",
                      "md_path": "2_测序/a.md", "file_name": "a.md",
                      "chapter": "2_测序", "node_name": "2_测序"}],
            "records": {"node_A::2_测序/a.md": legacy_record},
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(legacy_state, handle, ensure_ascii=False)

        store = StateStore(path, logger=self.logger)
        state = store.load()
        self.assertEqual(state.plan, [])
        self.assertEqual(state.records, {})
        self.assertEqual(store.completed_keys(), set())

        backups = [name for name in os.listdir(os.path.dirname(path))
                   if name.startswith("state.v1.") and name.endswith(".json")]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(os.path.dirname(path), backups[0]), "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["records"], legacy_state["records"])
        self.assertTrue(any("已从 v1 升级" in entry[1] for entry in self.logger.entries))

        store.upsert(ExecutionRecord(unique_key="new::1", status=TaskStatus.SUCCESS.value))
        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["schema_version"], 2)

    def test_n10_summary_total_is_sum_of_repeats_with_chapter_source(self) -> None:
        """N10：完成率分母=ΣM；by_chapter[章]={total:M, done:k, repeat_source}。"""
        plan = build_chapter_plan(
            self.tree, ["2_测序", "4_注释"], 0, [],
            repeat_default=3, repeat_overrides={"2_测序": 3, "4_注释": 2},
        )
        for task in plan:
            self.store.upsert(ExecutionRecord(
                unique_key=task.unique_key, status=TaskStatus.PENDING.value))
        seq = [task for task in plan if task.chapter == "2_测序"][:2]
        self.store.upsert(ExecutionRecord(
            unique_key=seq[0].unique_key, status=TaskStatus.SUCCESS.value, score=90.0))
        self.store.upsert(ExecutionRecord(
            unique_key=seq[1].unique_key, status=TaskStatus.SKIPPED.value, score=90.0))
        annotate = next(task for task in plan if task.chapter == "4_注释")
        self.store.upsert(ExecutionRecord(
            unique_key=annotate.unique_key, status=TaskStatus.SUCCESS.value, score=80.0))

        state = self.store.load()
        state.plan = plan
        summary = state.summary()
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["done"], 3)
        self.assertEqual(summary["by_chapter"]["2_测序"]["total"], 3)
        self.assertEqual(summary["by_chapter"]["2_测序"]["done"], 2)
        self.assertEqual(summary["by_chapter"]["4_注释"]["total"], 2)
        self.assertEqual(summary["by_chapter"]["4_注释"]["done"], 1)
        self.assertEqual(summary["by_chapter"]["2_测序"]["repeat_source"], "override")


if __name__ == "__main__":
    unittest.main()
