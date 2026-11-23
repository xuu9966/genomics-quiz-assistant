"""单任务/计划执行器：组卷 → 作答 → 交卷 → 轮询出分 → 落库 → 题库采集。

任何单任务异常都落入 FAILED 记录，不中断整个计划；所有网络调用经 PlatformClient。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import parsers
from answer_engine import AnswerEngine, PlatformChatAnswerEngine, selection_to_texts
from config import ConfigError
from models import AnswerDetail, AppConfig, ExecutionRecord, QType, Task, TaskStatus
from platform_client import (
    AuthError, GenerateError, NetworkError, Paper, PlatformClient, PlatformError,
    RateLimitError, RequestError, ServerError, SubmitError,
)
from state_store import StateStore
from utils import mask, now_iso, truncate


class AnswerFailure(Exception):
    """内部使用：作答未通过解析，需记 ANSWER_PARSE。"""

    error_type = "ANSWER_PARSE"


class ParseFailure(Exception):
    """内部使用：结果页解析/采集失败，需记 PARSE。"""

    error_type = "PARSE"


class TimeoutFailure(Exception):
    """内部使用：总超时或出分超时。"""

    error_type = "TIMEOUT"


class TaskExecutor:
    """严格单线程串行执行；run_plan 以生成器方式逐个产出记录。"""

    def __init__(self, client: PlatformClient, engine: AnswerEngine, store: StateStore,
                 bank: Any, logger: Any, config: AppConfig, force: bool = False,
                 sleep_fn: Any = None, clock: Any = None):
        self.client = client
        self.engine = engine or PlatformChatAnswerEngine(config.model)
        self.store = store
        self.bank = bank
        self.logger = logger
        self.config = config
        self.force = bool(force)
        self.force_keys: set = set()
        self._lock = threading.RLock()
        self._sleep = sleep_fn or time.sleep
        self._clock = clock or time.monotonic

    def _log(self, message: str, task_key: str = "", level: str = "INFO") -> None:
        if self.logger is not None and hasattr(self.logger, "log"):
            self.logger.log(level, truncate(message, 400), task_key)

    def _chat_fn(self, session_id: str = "") -> Any:
        """把平台问答绑定为 chat_fn(message)，模型来自配置。"""
        def _call(message: str) -> str:
            return self.client.chat(message, self.config.model)
        return _call

    def run_plan(self, plan: List[Task]) -> Iterator[ExecutionRecord]:
        """逐个执行；已完成（SUCCESS）任务标记为 SKIPPED，组卷次数为 0。"""
        state = self.store.merge_plan(plan, force=self.force)
        for task in plan:
            with self._lock:
                previous = state.records.get(task.unique_key) or self.store.get(task.unique_key)
                should_rerun = self.force or task.unique_key in self.force_keys
                if not should_rerun and previous and previous.status == TaskStatus.SUCCESS.value:
                    skipped = ExecutionRecord.from_dict(previous.to_dict())
                    skipped.status = TaskStatus.SKIPPED.value
                    skipped.finished_at = skipped.finished_at or now_iso()
                    self.store.upsert(skipped)
                    state.records[task.unique_key] = skipped
                    self._log("已有成功记录，跳过（不会调用组卷接口）。", task.unique_key)
                    yield skipped
                    continue
                if task.unique_key in self.force_keys:
                    self.store.clear([task.unique_key])
                    self.force_keys.discard(task.unique_key)
            record = self.run_task(task)
            state.records[task.unique_key] = record
            yield record

    def queue_rerun(self, keys: List[str]) -> int:
        """面板「重新执行」：登记待重跑的唯一键。"""
        with self._lock:
            for key in keys or []:
                if key:
                    self.force_keys.add(str(key))
            return len(self.force_keys)

    def run_task(self, task: Task) -> ExecutionRecord:
        """执行单任务；任何异常转换为 FAILED 记录后返回，不向外抛出。"""
        key = task.unique_key
        previous = self.store.get(key)
        record = ExecutionRecord(
            unique_key=key, status=TaskStatus.RUNNING.value,
            attempts=int(previous.attempts if previous else 0) + 1,
            started_at=now_iso(), finished_at="",
        )
        self.store.upsert(record)
        self._log(f"开始复习任务：{task.label}", key)
        deadline = self._clock() + max(1, int(self.config.task_timeout_sec))
        try:
            paper = self.generate(task)
            self._check_deadline(deadline)
            self._log(f"组卷完成，会话 {paper.session_id[:8]}…", key)

            answers = self.answer_all(paper)
            self._check_deadline(deadline)
            if not answers:
                # 兜底：空卷交上去平台会判 0 分且仍回"已评阅"，等于白跑一次还污染成绩。
                raise AnswerFailure("试卷没有任何题目，已中止交卷以避免产生 0 分记录。")
            if any(not detail.ok for detail in answers):
                failed_ids = ",".join(detail.question_id for detail in answers if not detail.ok)
                raise AnswerFailure(f"以下题目无法解析为明确选项：{truncate(failed_ids, 120)}")

            self.submit(task, paper, answers)
            self._check_deadline(deadline)
            self._log("交卷完成，等待评阅出分。", key)

            score, result_html = self.wait_and_collect(paper.session_id, task, deadline)
            if score is None:
                raise ParseFailure("结果页未找到最终得分/已评阅分数。")
            self.bank.collect_from_result(result_html, task.chapter)
            if hasattr(self.bank, "save"):
                # 每题做完立即落盘：中途关窗口也不会丢已经采到的题目与解析。
                self.bank.save()
            record.status = TaskStatus.SUCCESS.value
            record.score = float(score)
            record.session_id = paper.session_id
            record.error_type = ""
            record.error_summary = ""
            self._log(f"复习完成，得分 {score:.1f}。", key)
        except Exception as exc:
            error_type = self._error_type(exc)
            record.status = TaskStatus.FAILED.value
            record.error_type = error_type
            record.error_summary = f"{error_type}: {mask(truncate(str(exc), 240))}"
            self._log(f"任务失败（{error_type}）：{mask(truncate(str(exc), 200))}", key, "ERROR")
        record.finished_at = now_iso()
        self.store.upsert(record)
        return record

    @staticmethod
    def _error_type(exc: BaseException) -> str:
        error_type = getattr(exc, "error_type", None)
        if isinstance(error_type, str) and error_type:
            return error_type
        if isinstance(exc, ConfigError):
            return "CONFIG"
        if isinstance(exc, AuthError):
            return "AUTH"
        if isinstance(exc, RateLimitError):
            return "RATE_LIMIT"
        if isinstance(exc, ServerError):
            return "SERVER"
        if isinstance(exc, NetworkError):
            return "NETWORK"
        if isinstance(exc, GenerateError):
            return "GENERATE"
        if isinstance(exc, SubmitError):
            return "SUBMIT"
        if isinstance(exc, TimeoutError) or isinstance(exc, TimeoutFailure):
            return "TIMEOUT"
        if isinstance(exc, parsers.ParseError) or isinstance(exc, ParseFailure):
            return "PARSE"
        return "UNKNOWN"

    def _check_deadline(self, deadline: float) -> None:
        if self._clock() >= deadline:
            raise TimeoutFailure("单任务总耗时超过配置的 task_timeout_sec。")

    def generate(self, task: Task) -> Paper:
        """组卷；原样使用 md_path 与 node_id。"""
        return self.client.generate_paper(task.node_id, task.md_path, None)

    def answer_all(self, paper: Paper) -> List[AnswerDetail]:
        """逐题作答，单选/多选分别交给同一引擎解析。"""
        results: List[AnswerDetail] = []
        chat_fn = self._chat_fn(paper.session_id)
        for qtype, question in paper.all_questions():
            item = dict(question)
            item.setdefault("qtype", qtype)
            item.setdefault("session_id", paper.session_id)
            results.append(self.engine.answer(item, chat_fn))
        return results

    def submit(self, task: Task, paper: Paper, answers: List[AnswerDetail]) -> None:
        """按 §7.7 构造 task_data 并交卷。"""
        qtype_names = self.config.qtype_names()
        grouped: Dict[str, Dict[str, Any]] = {}
        items = paper.all_questions()
        for index, (qtype, question) in enumerate(items):
            detail = answers[index] if index < len(answers) else AnswerDetail()
            bucket = grouped.setdefault(qtype, {
                "count": 0,
                "answer_count": 0,
                "qtype_name": qtype_names.get(qtype, qtype),
                "score": int(self.config.paper.get("score_per", 10)),
                "questions": [],
            })
            options = question.get("options") or []
            option_list = [str(option or "") for option in options] if isinstance(options, list) else []
            selection = detail.final_selection or ""
            letters = [part.strip().upper() for part in str(selection).split(",") if part.strip()]
            selected_indexes: List[int] = []
            for letter in letters:
                if len(letter) == 1 and "A" <= letter <= "Z":
                    position = ord(letter) - ord("A")
                    if 0 <= position < len(option_list):
                        selected_indexes.append(position)
            texts = selection_to_texts(question, selection) if qtype == QType.MULTIPLE.value else (
                [option_list[selected_indexes[0]]] if selected_indexes else []
            )
            student_answer: Any = texts if qtype == QType.MULTIPLE.value else (texts[0] if texts else "")
            bucket["count"] += 1
            bucket["answer_count"] += 1
            bucket["questions"].append({
                "id": str(question.get("id") or ""),
                "selected": selected_indexes if qtype == QType.MULTIPLE.value else (
                    selected_indexes[0] + 1 if selected_indexes else 0
                ),
                "student_answer": student_answer,
                "comment_score": None,
            })
        payload = {
            "task_data": json.dumps(grouped, ensure_ascii=False),
            "switch_count": 0,
            "md_name": paper.md_name,
            "node_id": paper.node_id,
            "duration": paper.duration,
            "rules": paper.rules,
        }
        self.client.submit_paper(paper.session_id, payload)

    def wait_and_collect(self, session_id: str, task: Task,
                         deadline: Optional[float] = None) -> Tuple[Optional[float], str]:
        """轮询结果页出分；超时返回 (None, 最后页面)，由调用方记 TIMEOUT。"""
        poll_deadline = self._clock() + max(1, int(self.config.score_poll_timeout_sec))
        if deadline is not None:
            poll_deadline = min(poll_deadline, deadline)
        interval = max(1, int(self.config.score_poll_interval_sec))
        last_html = ""
        while True:
            if self._clock() >= poll_deadline:
                raise TimeoutFailure("出分轮询超过 score_poll_timeout_sec。")
            last_html = self.client.get_result_html(session_id)
            score = parsers.extract_score(last_html)
            if score is not None:
                return score, last_html
            self._log("评阅中，尚未出分，稍后再次查看。", task.unique_key)
            self._sleep(interval)

    def collect_conclusions(self, client: Optional[PlatformClient] = None, pages: int = 1) -> int:
        """从任务列表 conclusion 汇聚题库（只读接口，可与执行器分开调用）。"""
        source = client or self.client
        added = 0
        for page in range(1, max(1, int(pages)) + 1):
            data = source.list_tasks(page)
            tasks = data.get("tasks") or []
            total_pages = int(data.get("total_pages") or 1)
            for item in tasks:
                if not isinstance(item, dict):
                    continue
                conclusion = item.get("conclusion") or ""
                if not conclusion:
                    continue
                added += self.bank.collect_from_conclusion(conclusion, "")
            if page >= total_pages:
                break
        if added and hasattr(self.bank, "save"):
            self.bank.save()
        return added
