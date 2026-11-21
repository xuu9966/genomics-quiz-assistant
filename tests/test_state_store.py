"""state_store.py 用例：原子读写、五态、断点续跑判据、clear 强制重跑。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from models import ExecutionRecord, RunState, Task, TaskStatus
from state_store import StateStore


def _task(key: str, chapter: str = "2_测序") -> Task:
    return Task(unique_key=key, node_id=key.split("::")[0], md_path=key.split("::")[-1],
                file_name=os.path.basename(key), chapter=chapter)


class TestStateStorePersistence(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "data", "state.json")
        self.store = StateStore(self.path, config_masked={"password": "***"})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_save_load_roundtrip_contains_full_plan_and_records(self) -> None:
        state = RunState(
            config_masked={"password": "***"},
            plan=[_task("n1::2_测序/a.md"), _task("n2::2_测序/b.md")],
            records={"n1::2_测序/a.md": ExecutionRecord(
                unique_key="n1::2_测序/a.md", status=TaskStatus.SUCCESS.value,
                score=88.5, session_id="sess-abcdefgh")},
        )
        self.store.save(state)
        loaded = self.store.load()
        self.assertEqual(len(loaded.plan), 2)
        self.assertEqual(loaded.records["n1::2_测序/a.md"].score, 88.5)
        self.assertEqual(loaded.config_masked["password"], "***")
        self.assertTrue(os.path.exists(self.path))

    def test_upsert_then_get(self) -> None:
        record = ExecutionRecord(unique_key="k1", status=TaskStatus.FAILED.value,
                                 error_type="TIMEOUT", error_summary="超时")
        self.store.upsert(record)
        loaded = self.store.get("k1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.error_type, "TIMEOUT")
        self.assertEqual(loaded.status, "failed")

    def test_atomic_json_content_is_valid(self) -> None:
        self.store.upsert(ExecutionRecord(unique_key="k::a.md", status="pending"))
        with open(self.path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertIn("records", data)
        self.assertIn("plan", data)
        self.assertIn("summary", data)

    def test_corrupted_file_returns_empty_state(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ broken")
        state = self.store.load()
        self.assertEqual(state.plan, [])
        self.assertEqual(state.records, {})


class TestCompletedKeysAndRerun(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(os.path.join(self.tmp.name, "state.json"))
        self.keys = [f"n{i}::{i}.md" for i in range(5)]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed(self) -> None:
        statuses = ["success", "failed", "running", "skipped", "pending"]
        for key, status in zip(self.keys, statuses):
            self.store.upsert(ExecutionRecord(unique_key=key, status=status, score=90 if status == "success" else None))

    def test_completed_keys_only_success(self) -> None:
        self._seed()
        self.assertEqual(self.store.completed_keys(), {self.keys[0]})

    def test_clear_removes_record_for_forced_rerun(self) -> None:
        self._seed()
        self.store.clear([self.keys[0]])
        self.assertIsNone(self.store.get(self.keys[0]))
        self.assertEqual(self.store.completed_keys(), set())

    def test_merge_plan_keeps_pending_tasks_and_records(self) -> None:
        self._seed()
        plan = [_task(self.keys[0]), _task(self.keys[4]), _task("new::new.md")]
        state = self.store.merge_plan(plan, force=False)
        self.assertEqual([t.unique_key for t in state.plan], [self.keys[0], self.keys[4], "new::new.md"])
        self.assertEqual(len(state.records), 5)

    def test_merge_plan_force_drops_success_records(self) -> None:
        self._seed()
        plan = [_task(key) for key in self.keys[:2]]
        state = self.store.merge_plan(plan, force=True)
        self.assertEqual(len(state.plan), 2)
        # force 只清除本次纳入计划的成功记录；计划外历史记录保留，避免误删
        for key in self.keys[:2]:
            self.assertNotIn(key, state.records)
        for key in self.keys[2:]:
            self.assertIn(key, state.records)

    def test_five_status_values_roundtrip(self) -> None:
        for status in ("pending", "running", "success", "failed", "skipped"):
            self.store.upsert(ExecutionRecord(unique_key=f"k::{status}", status=status))
        self.assertEqual(len(self.store.load().records), 5)


if __name__ == "__main__":
    unittest.main()
