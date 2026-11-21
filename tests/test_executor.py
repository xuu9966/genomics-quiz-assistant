"""executor.py 用例（全程假 transport / 假 chat，绝不触网）：

完整流程、失败隔离、M5 断点续跑组卷次数 0、超时、task_data 契约。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from answer_engine import PlatformChatAnswerEngine
from bank import QuestionBank
from executor import TaskExecutor
from fixtures import CONCLUSION_JSON, GENERATE_RESPONSE, RESULT_HTML, RESULT_HTML_NO_SCORE, config_copy, normalized_questions
from models import AppConfig, ExecutionRecord, Task, TaskStatus
from platform_client import GenerateError, Paper
from state_store import StateStore


class FakeClient:
    """平台适配层替身：仅记录调用，不发起任何网络请求。"""

    def __init__(self, responses=None, result_html=RESULT_HTML, fail_first_generate=False):
        self.generate_calls = []
        self.submit_calls = []
        self.result_calls = 0
        self.chat_calls = []
        self.list_task_calls = []
        self.chat_responses = list(responses or ["B", "A,C"])
        self.result_html = result_html
        self.fail_first_generate = fail_first_generate
        data = GENERATE_RESPONSE
        self.paper = Paper(
            session_id=data["session_id"], redirect=data["redirect"],
            md_name=data["md_name"], node_id=data["node_id"],
            task_name=data["task_name"], duration=data["duration"],
            # 夹具已改为平台真实的「题型 → 分桶对象」结构，这里必须走同一套归一化，
            # 否则替身会像老代码那样把题目解析成 0 道。
            rules=list(data["rules"]), questions=normalized_questions(),
        )

    def generate_paper(self, node_id, md_path, paper=None):
        self.generate_calls.append((node_id, md_path))
        if self.fail_first_generate and len(self.generate_calls) == 1:
            raise GenerateError("组卷服务暂时不可用")
        return self.paper

    def chat(self, message, model=None):
        self.chat_calls.append((message, model))
        index = len(self.chat_calls) - 1
        return self.chat_responses[index] if index < len(self.chat_responses) else "B"

    def submit_paper(self, session_id, payload):
        self.submit_calls.append((session_id, payload))

    def get_result_html(self, session_id):
        self.result_calls += 1
        return self.result_html

    def list_tasks(self, page=1):
        self.list_task_calls.append(page)
        return {"tasks": [{"conclusion": CONCLUSION_JSON}], "total_pages": 1}


class StepClock:
    """可注入时钟：每次调用前进 step，用于确定性验证超时分支。"""

    def __init__(self, step=0.0, start=0.0):
        self.value = float(start)
        self.step = float(step)

    def __call__(self):
        current = self.value
        self.value += self.step
        return current


class FakeLogger:
    def __init__(self):
        self.entries = []

    def log(self, level, message, task_key=""):
        self.entries.append((level, message, task_key))


def _task(key="node_A::2_测序/基因组测序.md", node_id="node_A"):
    return Task(unique_key=key, node_id=node_id, md_path=key.split("::", 1)[1],
                file_name=os.path.basename(key), chapter="2_测序")


class ExecutorTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(os.path.join(self.tmp.name, "state.json"))
        self.config = AppConfig._from_dict(config_copy())
        self.bank = QuestionBank()
        self.logger = FakeLogger()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_executor(self, client, **kwargs):
        return TaskExecutor(
            client=client, engine=PlatformChatAnswerEngine(), store=self.store,
            bank=self.bank, logger=self.logger, config=self.config,
            sleep_fn=lambda _: None, clock=kwargs.pop("clock", StepClock()), **kwargs,
        )


class TestFullFlow(ExecutorTestBase):
    def test_generate_answer_submit_score_and_persist(self) -> None:
        # Arrange
        client = FakeClient()
        executor = self.make_executor(client)
        task = _task()
        # Act
        records = list(executor.run_plan([task]))
        # Assert：状态与分数落库
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.status, TaskStatus.SUCCESS.value)
        self.assertEqual(record.score, 90.0)
        self.assertEqual(record.session_id, GENERATE_RESPONSE["session_id"])
        stored = self.store.get(task.unique_key)
        self.assertEqual(stored.status, TaskStatus.SUCCESS.value)
        self.assertEqual(stored.score, 90.0)
        # Assert：组卷 / 作答 / 交卷 / 查分各就各位
        self.assertEqual(client.generate_calls, [("node_A", "2_测序/基因组测序.md")])
        self.assertEqual(len(client.chat_calls), 2)
        self.assertEqual(len(client.submit_calls), 1)
        self.assertEqual(client.result_calls, 1)
        # Assert：题库采集
        self.assertEqual(len(self.bank), 2)
        # Assert：面板可见 RUNNING→SUCCESS 日志
        self.assertTrue(any("复习完成" in item[1] for item in self.logger.entries))

    def test_task_data_fields_follow_contract(self) -> None:
        client = FakeClient(responses=["B", "A,C"])
        self.make_executor(client).run_task(_task())
        session_id, payload = client.submit_calls[0]
        self.assertEqual(session_id, GENERATE_RESPONSE["session_id"])
        self.assertEqual(payload["switch_count"], 0)
        self.assertEqual(payload["md_name"], GENERATE_RESPONSE["md_name"])
        self.assertEqual(payload["node_id"], GENERATE_RESPONSE["node_id"])
        self.assertEqual(payload["duration"], GENERATE_RESPONSE["duration"])
        self.assertEqual(payload["rules"], GENERATE_RESPONSE["rules"])
        task_data = json.loads(payload["task_data"])
        self.assertEqual(set(task_data.keys()), {"qt01", "qt02"})
        single = task_data["qt01"]
        multiple = task_data["qt02"]
        self.assertEqual(single["qtype_name"], "单选题")
        self.assertEqual(multiple["qtype_name"], "多选题")
        self.assertEqual(single["count"], 1)
        self.assertEqual(single["answer_count"], 1)
        self.assertIsInstance(single["questions"][0]["student_answer"], str)
        self.assertEqual(single["questions"][0]["student_answer"], "PCR扩增")
        self.assertEqual(single["questions"][0]["selected"], 2)
        self.assertEqual(single["questions"][0]["comment_score"], None)
        self.assertIsInstance(multiple["questions"][0]["student_answer"], list)
        self.assertEqual(multiple["questions"][0]["student_answer"], ["贪心算法", "OLC"])
        self.assertEqual(multiple["questions"][0]["selected"], [0, 2])


class TestFailureIsolation(ExecutorTestBase):
    def test_one_task_failure_does_not_stop_the_plan(self) -> None:
        client = FakeClient(fail_first_generate=True)
        executor = self.make_executor(client)
        first = _task()
        second = _task("node_B::3_组装/从头组装.md", "node_B")
        records = list(executor.run_plan([first, second]))
        self.assertEqual([r.status for r in records],
                         [TaskStatus.FAILED.value, TaskStatus.SUCCESS.value])
        self.assertEqual(records[0].error_type, "GENERATE")
        self.assertIn("组卷服务暂时不可用", records[0].error_summary)
        self.assertEqual(len(client.generate_calls), 2)

    def test_answer_parse_failure_marks_task_failed(self) -> None:
        client = FakeClient(responses=["无法判断", "无法判断"])
        record = self.make_executor(client).run_task(_task())
        self.assertEqual(record.status, TaskStatus.FAILED.value)
        self.assertEqual(record.error_type, "ANSWER_PARSE")
        self.assertEqual(client.submit_calls, [])

    def test_empty_paper_never_submitted(self) -> None:
        """0 题空卷必须中止：真机事故里空卷被平台判 0 分却记 success。"""
        client = FakeClient()
        client.paper.questions = {}
        record = self.make_executor(client).run_task(_task())
        self.assertEqual(record.status, TaskStatus.FAILED.value)
        self.assertEqual(record.error_type, "ANSWER_PARSE")
        self.assertEqual(client.submit_calls, [])
        self.assertEqual(client.result_calls, 0)


class TestResumeAndRerun(ExecutorTestBase):
    def test_m5_success_task_generate_call_count_is_zero(self) -> None:
        done = _task("node_A::2_测序/已完成.md", "node_A")
        todo = _task("node_B::2_测序/待完成.md", "node_B")
        self.store.upsert(ExecutionRecord(
            unique_key=done.unique_key, status=TaskStatus.SUCCESS.value,
            score=95.0, session_id="sess-done-0001"))
        client = FakeClient()
        executor = self.make_executor(client)
        records = list(executor.run_plan([done, todo]))
        self.assertEqual(records[0].status, TaskStatus.SKIPPED.value)
        self.assertEqual(records[0].score, 95.0)
        self.assertEqual(records[1].status, TaskStatus.SUCCESS.value)
        # 关键指标：只为未完成的一次组卷，已完成任务组卷调用次数 = 0
        self.assertEqual(client.generate_calls, [("node_B", "2_测序/待完成.md")])
        self.assertEqual(len(client.generate_calls), 1)

    def test_queue_rerun_regenerates_success_task(self) -> None:
        task = _task()
        self.store.upsert(ExecutionRecord(
            unique_key=task.unique_key, status=TaskStatus.SUCCESS.value, score=80.0))
        client = FakeClient()
        executor = self.make_executor(client)
        executor.queue_rerun([task.unique_key])
        records = list(executor.run_plan([task]))
        self.assertEqual(records[0].status, TaskStatus.SUCCESS.value)
        self.assertEqual(len(client.generate_calls), 1)

    def test_force_flag_ignores_success_record(self) -> None:
        task = _task()
        self.store.upsert(ExecutionRecord(
            unique_key=task.unique_key, status=TaskStatus.SUCCESS.value, score=80.0))
        client = FakeClient()
        executor = self.make_executor(client, force=True)
        list(executor.run_plan([task]))
        self.assertEqual(len(client.generate_calls), 1)

    def test_collect_conclusions_uses_read_only_list_api(self) -> None:
        client = FakeClient()
        executor = self.make_executor(client)
        added = executor.collect_conclusions(client, pages=1)
        self.assertGreater(added, 0)
        self.assertEqual(client.generate_calls, [])
        self.assertEqual(client.submit_calls, [])
        self.assertEqual(client.list_task_calls, [1])


class TestTimeout(ExecutorTestBase):
    def test_total_timeout_recorded_as_timeout(self) -> None:
        self.config.task_timeout_sec = 1
        client = FakeClient()
        record = self.make_executor(client, clock=StepClock(step=2)).run_task(_task())
        self.assertEqual(record.status, TaskStatus.FAILED.value)
        self.assertEqual(record.error_type, "TIMEOUT")
        self.assertEqual(client.submit_calls, [])

    def test_score_poll_timeout_recorded_as_timeout(self) -> None:
        self.config.task_timeout_sec = 1000
        self.config.score_poll_timeout_sec = 1
        client = FakeClient(result_html=RESULT_HTML_NO_SCORE)
        record = self.make_executor(client, clock=StepClock(step=2)).run_task(_task())
        self.assertEqual(record.status, TaskStatus.FAILED.value)
        self.assertEqual(record.error_type, "TIMEOUT")
        # 交卷发生在出分轮询之前，超时只影响查分与落库，不影响已交卷事实
        self.assertEqual(len(client.submit_calls), 1)
        self.assertIsNone(record.score)


if __name__ == "__main__":
    unittest.main()
