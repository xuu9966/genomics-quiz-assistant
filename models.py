"""领域数据类与枚举（设计文档 §3.1 / §3.2）。"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import config as config_module
from utils import (
    chapter_from_path, make_task_key, make_unique_key, md5_text, normalize_md_path, now_iso,
)


class TaskStatus(str, Enum):
    """任务状态；落盘值为小写字符串。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class QType(str, Enum):
    """平台题型 id。"""

    SINGLE = "qt01"
    MULTIPLE = "qt02"


@dataclass
class AppConfig:
    """应用配置；password 仅内存持有，save() 永远不会写入。"""

    base_url: str = ""
    username: str = ""
    password: str = ""
    password_env: str = "CQA_PASSWORD"
    chapters: List[str] = field(default_factory=list)
    limit: int = 10
    repeat_default: int = 3
    repeat_overrides: Dict[str, int] = field(default_factory=dict)
    model: str = "deepseek-v4-flash"
    paper: Dict[str, Any] = field(default_factory=lambda: {
        "single_count": 5, "multi_count": 5, "score_per": 10, "duration": 0,
    })
    request_interval_sec: float = 2.0
    retry_times: int = 3
    retry_backoff_sec: float = 2.0
    task_timeout_sec: int = 180
    score_poll_interval_sec: int = 5
    score_poll_timeout_sec: int = 90
    web: Dict[str, Any] = field(default_factory=lambda: {
        "host": "127.0.0.1", "port": 8765, "poll_sec": 3, "status_color": False,
    })
    paths: Dict[str, Any] = field(default_factory=lambda: {
        "data_dir": "data", "log_dir": "logs", "export_dir": "exports",
    })

    @classmethod
    def load(cls, path: str, require_password: bool = True, apply_env: bool = True) -> "AppConfig":
        """从文件 + 环境变量加载配置；缺密码时按环境变量/getpass 获取。"""
        data = config_module.load_config(path)
        if apply_env:
            config_module.apply_env_overrides(data)
        # 网页启动（require_password=False）模式下，学号/密码由网页表单提供，
        # 故此处不强制 username 必填；命令行路径保持原有强校验。
        config_module.validate_config(data, require_password=False,
                                      require_username=require_password)
        instance = cls._from_dict(data)
        if require_password and not instance.password:
            instance.password = config_module.prompt_password(instance.password_env)
        if require_password:
            instance.validate()
        return instance

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        return cls(
            base_url=str(data.get("base_url") or ""),
            username=str(data.get("username") or ""),
            password=str(data.get("password") or ""),
            password_env=str(data.get("password_env") or "CQA_PASSWORD"),
            chapters=list(data.get("chapters") or []),
            limit=int(data.get("limit", 10)),
            repeat_default=int(data.get("repeat_default", 3)),
            repeat_overrides=dict(data.get("repeat_overrides") or {}),
            model=str(data.get("model") or "deepseek-v4-flash"),
            paper=dict(data.get("paper") or {}),
            request_interval_sec=float(data.get("request_interval_sec", 2.0)),
            retry_times=int(data.get("retry_times", 3)),
            retry_backoff_sec=float(data.get("retry_backoff_sec", 2.0)),
            task_timeout_sec=int(data.get("task_timeout_sec", 180)),
            score_poll_interval_sec=int(data.get("score_poll_interval_sec", 5)),
            score_poll_timeout_sec=int(data.get("score_poll_timeout_sec", 90)),
            web=dict(data.get("web") or {}),
            paths=dict(data.get("paths") or {}),
        )

    def to_dict(self, include_password: bool = False) -> Dict[str, Any]:
        """转换为字典；默认排除密码。"""
        value = asdict(self)
        if not include_password:
            value.pop("password", None)
        return value

    def save(self, path: str) -> None:
        """保存配置，强制不写密码。"""
        config_module.save_config(path, self.to_dict(include_password=False))

    def apply_env_overrides(self) -> None:
        """将当前环境变量覆盖到本实例。"""
        data = config_module.apply_env_overrides(self.to_dict(include_password=True))
        updated = self._from_dict(data)
        for key in ("base_url", "username", "password", "password_env", "chapters", "limit",
                    "repeat_default", "repeat_overrides", "model", "paper",
                    "request_interval_sec", "retry_times", "retry_backoff_sec",
                    "task_timeout_sec", "score_poll_interval_sec", "score_poll_timeout_sec",
                    "web", "paths"):
            setattr(self, key, getattr(updated, key))

    def validate(self) -> None:
        """校验配置；缺密码时可读报错。"""
        config_module.validate_config(self.to_dict(include_password=True), require_password=True)

    def safe_dict(self) -> Dict[str, Any]:
        """脱敏视图，可安全写入 state.json / 日志 / 面板。"""
        return config_module.masked_view(self.to_dict(include_password=True))

    def paper_items(self) -> List[Dict[str, Any]]:
        """组卷 config 数组（§7.7）。"""
        return [
            {"qtype_id": QType.SINGLE.value, "count": int(self.paper.get("single_count", 5)),
             "score": int(self.paper.get("score_per", 10))},
            {"qtype_id": QType.MULTIPLE.value, "count": int(self.paper.get("multi_count", 5)),
             "score": int(self.paper.get("score_per", 10))},
        ]

    def qtype_names(self) -> Dict[str, str]:
        """题型 id → 名称，用于构造交卷数据。"""
        return {QType.SINGLE.value: "单选题", QType.MULTIPLE.value: "多选题"}


@dataclass
class Task:
    """一个复习执行单元；v2 语义为（顶层大章 C, 第 k 次）。

    v1 的逐文件任务仍可由 from_node_and_path 构造，两个新字段带默认值以兼容旧数据与用例。
    """

    unique_key: str = ""
    node_id: str = ""
    md_path: str = ""
    file_name: str = ""
    chapter: str = ""
    node_name: str = ""
    repeat_index: int = 1
    repeat_source: str = "default"

    @classmethod
    def from_node_and_path(cls, node: Dict[str, Any], path: str, chapter_hint: str = "") -> "Task":
        """由树节点与文件路径构造任务（v1 legacy，保持不变）。"""
        node_id = str((node or {}).get("id", "") or "").strip()
        node_name = str((node or {}).get("name", "") or "")
        md_path = str(path or "")
        chapter = chapter_from_path(md_path, fallback=node_name or chapter_hint)
        return cls(
            unique_key=make_unique_key(node_id, md_path),
            node_id=node_id,
            md_path=md_path,
            file_name=os.path.basename(normalize_md_path(md_path)),
            chapter=chapter,
            node_name=node_name,
        )

    @classmethod
    def from_chapter_and_repeat(
        cls, chapter_node: Dict[str, Any], md_path: str, k: int,
        source: str, chapter_name: str = "",
    ) -> "Task":
        """由顶层大章节点与第 k 次构造执行单元。

        同章所有 k 共享同一个大章 node_id；md_path 仅为代表文件，不影响题目归属。
        """
        node = dict(chapter_node or {})
        node_id = str(node.get("id", "") or "").strip()
        name = chapter_name or str(node.get("name", "") or "")
        return cls(
            unique_key=make_task_key(node_id, k),
            node_id=node_id,
            md_path=str(md_path or ""),
            file_name=os.path.basename(normalize_md_path(md_path)),
            chapter=name,
            node_name=name,
            repeat_index=int(k),
            repeat_source=str(source or "default"),
        )

    @property
    def label(self) -> str:
        """展示文案：'大章名 第k次'；无次数时回退代表文件名或节点名。"""
        if self.repeat_index:
            return f"{self.chapter or self.node_name} 第{self.repeat_index}次"
        return self.file_name or self.node_name

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["label"] = self.label
        return value

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Task":
        value = dict(data or {})
        payload = {k: value.get(k, "") for k in
                   ("unique_key", "node_id", "md_path", "file_name", "chapter", "node_name")}
        try:
            payload["repeat_index"] = int(value.get("repeat_index", 1) or 1)
        except (TypeError, ValueError):
            payload["repeat_index"] = 1
        payload["repeat_source"] = str(value.get("repeat_source") or "default")
        return cls(**payload)


@dataclass
class ExecutionRecord:
    """单任务执行记录，成功含分数/会话，失败含错误类型/摘要。"""

    unique_key: str = ""
    status: str = TaskStatus.PENDING.value
    session_id: str = ""
    score: Optional[float] = None
    error_type: str = ""
    error_summary: str = ""
    attempts: int = 0
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutionRecord":
        value = dict(data or {})
        return cls(
            unique_key=str(value.get("unique_key") or ""),
            status=str(value.get("status") or TaskStatus.PENDING.value),
            session_id=str(value.get("session_id") or ""),
            score=value.get("score"),
            error_type=str(value.get("error_type") or ""),
            error_summary=str(value.get("error_summary") or ""),
            attempts=int(value.get("attempts") or 0),
            started_at=str(value.get("started_at") or ""),
            finished_at=str(value.get("finished_at") or ""),
        )


@dataclass
class Question:
    """复习题库基本单元；dedup_key = md5(题干原文)。"""

    dedup_key: str = ""
    qid: str = ""
    qtype: str = QType.SINGLE.value
    content: str = ""
    options: List[str] = field(default_factory=list)
    correct_answer: str = ""
    analysis: str = ""
    sources: List[str] = field(default_factory=list)
    has_image: bool = False
    chapter: str = ""

    def __post_init__(self) -> None:
        if not self.dedup_key:
            self.dedup_key = md5_text(self.content)

    def merge(self, other: "Question") -> None:
        """同题多来源合并：来源/解析不丢失，空字段互相补齐。"""
        for source in other.sources:
            if source and source not in self.sources:
                self.sources.append(source)
        if other.analysis and other.analysis not in self.analysis:
            self.analysis = (self.analysis + "\n\n" + other.analysis).strip() if self.analysis else other.analysis
        if not self.correct_answer:
            self.correct_answer = other.correct_answer
        if not self.options:
            self.options = list(other.options)
        if not self.qid:
            self.qid = other.qid
        if not self.chapter:
            self.chapter = other.chapter
        self.has_image = bool(self.has_image or other.has_image)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AnswerDetail:
    """单题作答详情，保留原始返回用于复盘。"""

    session_id: str = ""
    question_id: str = ""
    raw_response: str = ""
    final_selection: str = ""
    selected_text: str = ""
    comment_score: float = 0.0
    ok: bool = False


@dataclass
class RunState:
    """面板一次读取即得到完整计划 + 记录。"""

    config_masked: Dict[str, Any] = field(default_factory=dict)
    plan: List[Task] = field(default_factory=list)
    records: Dict[str, ExecutionRecord] = field(default_factory=dict)
    updated_at: str = now_iso()

    def summary(self) -> Dict[str, Any]:
        """汇总指标（完成率分母 = 限量后的本次计划）。"""
        total = len(self.plan)
        keys = [task.unique_key for task in self.plan]
        done = success = failed = running = pending = skipped = 0
        scores: List[float] = []
        chapters: Dict[str, Dict[str, Any]] = {}

        for task in self.plan:
            chapter = task.chapter or "未分类"
            source = task.repeat_source or "default"
            bucket = chapters.setdefault(chapter, {
                "total": 0, "done": 0, "rate": 0.0, "repeat_source": source,
            })
            if source == "override":
                bucket["repeat_source"] = "override"
            bucket["total"] += 1
            record = self.records.get(task.unique_key)
            status = record.status if record else TaskStatus.PENDING.value
            if status == TaskStatus.SUCCESS.value:
                success += 1
                done += 1
                bucket["done"] += 1
                if record and record.score is not None:
                    scores.append(float(record.score))
            elif status == TaskStatus.SKIPPED.value:
                skipped += 1
                done += 1
                bucket["done"] += 1
                if record and record.score is not None:
                    scores.append(float(record.score))
            elif status == TaskStatus.FAILED.value:
                failed += 1
            elif status == TaskStatus.RUNNING.value:
                running += 1
            else:
                pending += 1

        for bucket in chapters.values():
            bucket["rate"] = round(100.0 * bucket["done"] / bucket["total"], 1) if bucket["total"] else 0.0

        return {
            "total": total,
            "done": done,
            "success": success,
            "skipped": skipped,
            "failed": failed,
            "running": running,
            "pending": pending,
            "average_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
            "full_scores": sum(1 for score in scores if score >= 100.0),
            "completion_rate": round(100.0 * done / total, 1) if total else 0.0,
            "by_chapter": chapters,
            "tracked_keys": keys,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config_masked": self.config_masked,
            "plan": [task.to_dict() for task in self.plan],
            "records": {key: record.to_dict() for key, record in self.records.items()},
            "updated_at": self.updated_at,
            "summary": self.summary(),
        }
