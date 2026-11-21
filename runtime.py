"""网页启动运行态管理：配置分层合并、幂等、单一后台线程与脱敏快照。

v3 增量旁路模块：不进入 executor.run_plan / StateStore 的既有调用链，
只在内存持有一次执行的 RunSession 与临时 AppConfig 副本。
密码唯一合法落点为本模块中的 cfg.password 内存副本与 HTTP 请求体；
任何快照、状态响应、日志与磁盘文件都不包含密码。
"""
from __future__ import annotations

import copy
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

import parsers
from answer_engine import PlatformChatAnswerEngine
from config import ConfigError, REPEAT_LIMIT
from executor import TaskExecutor
from models import AppConfig
from platform_client import PlatformClient
from planner import build_chapter_plan
from utils import mask, now_iso, truncate


# 顶层大章权威清单（site-verification-2026-11-21 §7.4）：仅用于表单校验与展示，
# 解析/规划逻辑仍以平台返回的树结构为准（planner/parsers 完全不改）。
CHAPTER_NAMES: Tuple[str, ...] = (
    "0_预习", "1_绪论", "2_测序", "3_组装", "4_注释", "5_数据库", "6_实验", "9_复习",
)
CHAPTER_NODE_IDS: Tuple[str, ...] = (
    "20260907180845_0001", "20260907180845_0002", "20260907180845_0005",
    "20260907180845_0008", "20260907180845_0017", "20260907180845_0030",
    "20260907180845_0034", "20260907180845_0044",
)

# 字段级来源标签：环境变量名（repeat_overrides 按 v2 结论不进环境变量）。
_ENV_BY_FIELD: Dict[str, str] = {
    "username": "CQA_USERNAME",
    "password": "CQA_PASSWORD",
    "chapters": "CQA_CHAPTERS",
    "repeat_default": "CQA_REPEAT_DEFAULT",
    "limit": "CQA_LIMIT",
}

# 快照白名单字段；password 刻意不在此清单中。
SNAPSHOT_FIELDS: Tuple[str, ...] = (
    "username", "chapters", "repeat_default", "repeat_overrides", "limit",
)


class RunPhase(str, Enum):
    """网页启动的五相位运行态。"""

    NOT_STARTED = "not_started"
    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


PHASE_TEXT: Dict[RunPhase, str] = {
    RunPhase.NOT_STARTED: "未启动",
    RunPhase.STARTING: "启动中",
    RunPhase.RUNNING: "执行中",
    RunPhase.COMPLETED: "已完成",
    RunPhase.FAILED: "失败",
}

_ACTIVE_PHASES = (RunPhase.STARTING, RunPhase.RUNNING)


@dataclass
class RunSession:
    """一次网页启动的运行态；to_dict 为白名单输出，绝不包含密码。"""

    phase: RunPhase = RunPhase.NOT_STARTED
    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    error_type: str = ""
    error_summary: str = ""
    snapshot: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """返回脱敏、深拷贝的面板视图；字段清单固定，新增密码字段也不会外泄。"""
        phase = self.phase if isinstance(self.phase, RunPhase) else RunPhase.NOT_STARTED
        return {
            "phase": phase.value,
            "phase_text": PHASE_TEXT.get(phase, "未启动"),
            "run_id": str(self.run_id or ""),
            "started_at": str(self.started_at or ""),
            "finished_at": str(self.finished_at or ""),
            "error_type": str(self.error_type or ""),
            "error_summary": str(self.error_summary or ""),
            "snapshot": copy.deepcopy(self.snapshot or {}),
        }


class RunManager:
    """单进程单实例的网页启动管理器；所有可变状态受 RLock 保护。

    构造所需的工厂钩子均为可选关键字参数，默认值与 main.py 的 build_* 完全一致；
    测试可注入假 client/executor 控制时序，不会影响生产路径。
    """

    def __init__(
        self,
        base_config: AppConfig,
        store: Any,
        bank: Any,
        logger: Any,
        *,
        client_factory: Optional[Callable[[AppConfig], Any]] = None,
        chat_engine_factory: Optional[Callable[[AppConfig], Any]] = None,
        executor_factory: Optional[Callable[[AppConfig, Any, Any], Any]] = None,
        plan_builder: Optional[Callable[..., List[Any]]] = None,
        graph_parser: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.base_config: AppConfig = copy.deepcopy(base_config)
        self.store = store
        self.bank = bank
        self.logger = logger
        self._lock = threading.RLock()
        self._session = RunSession(phase=RunPhase.NOT_STARTED)
        self._thread: Optional[threading.Thread] = None
        self._client_factory = client_factory or self._default_client
        self._chat_engine_factory = chat_engine_factory or self._default_chat_engine
        self._executor_factory = executor_factory or self._default_executor
        self._plan_builder = plan_builder or build_chapter_plan
        self._graph_parser = graph_parser or parsers.parse_graph_data

    # ---------- 默认工厂（生产路径） ----------
    def _default_client(self, cfg: AppConfig) -> Any:
        return PlatformClient(cfg, logger=self.logger,
                              request_timeout=float(max(30, cfg.task_timeout_sec)))

    @staticmethod
    def _default_chat_engine(cfg: AppConfig) -> Any:
        return PlatformChatAnswerEngine(cfg.model)

    def _default_executor(self, cfg: AppConfig, client: Any, engine: Any) -> Any:
        return TaskExecutor(client, engine, self.store, self.bank, self.logger, cfg)

    # ---------- 公共 API ----------
    def start(self, form: Dict[str, Any]) -> Dict[str, Any]:
        """受理一次网页启动；活跃期重复提交返回 ALREADY_RUNNING，不新建线程。"""
        with self._lock:
            if self._session.phase in _ACTIVE_PHASES:
                return {
                    "ok": False,
                    "code": "ALREADY_RUNNING",
                    "message": "已有复习任务在执行中，请等待完成。",
                }
            try:
                self._validate_form(form)
                cfg, snapshot = self._merge_config(form)
            except (ValueError, ConfigError) as exc:
                return {
                    "ok": False,
                    "code": "INVALID_INPUT",
                    "message": mask(truncate(str(exc), 200)) or "提交参数不合法。",
                }
            run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            session = RunSession(
                phase=RunPhase.STARTING,
                run_id=run_id,
                started_at=now_iso(),
                snapshot=snapshot,
            )
            self._session = session
            self._thread = threading.Thread(
                target=self._worker,
                args=(cfg, session),
                name="cqa-web-exec",
                daemon=True,
            )
            self._thread.start()
            return {
                "ok": True,
                "code": "STARTED",
                "message": "已启动复习，可在总览查看进度。",
                "run_id": run_id,
                "snapshot": copy.deepcopy(snapshot),
            }

    def snapshot(self) -> Dict[str, Any]:
        """返回当前运行态的脱敏副本，供 /api/state 的 runtime 键使用。"""
        with self._lock:
            return self._session.to_dict()

    # ---------- 校验与配置合并 ----------
    def _validate_form(self, form: Dict[str, Any]) -> None:
        """表单结构与取值范围校验；任何问题抛 ValueError（可读文案，无堆栈）。"""
        if not isinstance(form, dict):
            raise ValueError("提交参数必须是对象。")

        username = str(form.get("username") or "").strip()
        if not username:
            raise ValueError("学号不能为空。")

        password = form.get("password")
        if password is None:
            password = ""
        if not isinstance(password, str):
            raise ValueError("密码必须是文本。")
        # 密码为空时允许回落模板/环境变量中的值，最终非空由 cfg.validate() 把关。

        chapters = form.get("chapters")
        if chapters is None:
            chapters = []
        if not isinstance(chapters, list) or any(not isinstance(item, str) for item in chapters):
            raise ValueError("章节必须是字符串数组。")
        for item in chapters:
            name = str(item).strip()
            if name and not self._is_valid_chapter(name):
                raise ValueError(f"章节“{mask(truncate(name, 40))}”不在可选的大章范围内。")

        repeat_default = form.get("repeat_default")
        if (isinstance(repeat_default, bool) or not isinstance(repeat_default, int)
                or not (1 <= repeat_default <= REPEAT_LIMIT)):
            raise ValueError(f"每章次数须为 1~{REPEAT_LIMIT} 的整数。")

        overrides = form.get("repeat_overrides")
        if overrides is None:
            overrides = {}
        if not isinstance(overrides, dict):
            raise ValueError("按章覆盖必须是对象。")
        for name, value in overrides.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("按章覆盖的章名不能为空。")
            if not self._is_valid_chapter(str(name).strip()):
                raise ValueError(f"按章覆盖的章节“{mask(truncate(str(name), 40))}”不在可选范围内。")
            if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= REPEAT_LIMIT):
                raise ValueError(f"按章覆盖次数须为 1~{REPEAT_LIMIT} 的整数。")

        limit = form.get("limit")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("限量必须为非负整数（0 表示全量）。")

    @staticmethod
    def _is_valid_chapter(name: str) -> bool:
        """合法章节 = 权威章名 / 权威节点 id / 章名前缀（如“2”“2_测”）。"""
        text = str(name or "").strip()
        if not text:
            return False
        if text in CHAPTER_NAMES or text in CHAPTER_NODE_IDS:
            return True
        for chapter in CHAPTER_NAMES:
            if chapter.startswith(text):
                return True
        return False

    @staticmethod
    def _env_value(field: str) -> Any:
        """读取并解析字段对应的环境变量；未设置或非法返回 None。"""
        from config import re_split_chapters

        env_name = _ENV_BY_FIELD.get(field)
        raw = str(os.environ.get(env_name) or "").strip() if env_name else ""
        if not raw:
            return None
        if field == "chapters":
            return [item.strip() for item in re_split_chapters(raw) if item.strip()]
        if field in ("repeat_default", "limit"):
            try:
                return int(raw)
            except ValueError:
                return None
        return raw

    def _merge_config(self, form: Dict[str, Any]) -> Tuple[AppConfig, Dict[str, Any]]:
        """网页 > 环境变量 > 模板：仅覆盖 6 个字段，生成字段级来源快照，绝不回写文件。"""
        base = copy.deepcopy(self.base_config)
        cfg = copy.deepcopy(base)
        web = dict(form or {})

        # 环境变量层：cmd_serve 时 base 通常已由 AppConfig.load 应用过，这里再叠一次是幂等的，
        # 从而保证无论调用方如何构造 base，"环境变量 > 模板"都成立。仍不回写文件。
        env_values: Dict[str, Any] = {}
        for field in SNAPSHOT_FIELDS + ("password",):
            parsed = self._env_value(field)
            if parsed is not None:
                env_values[field] = parsed
                setattr(cfg, field, parsed)

        snapshot: Dict[str, Any] = {
            field: {
                "value": copy.deepcopy(getattr(cfg, field, None)),
                "source": "env" if field in env_values else "template",
            }
            for field in SNAPSHOT_FIELDS
        }

        username = str(web.get("username") or "").strip()
        if username:
            cfg.username = username
            snapshot["username"] = {"value": username, "source": "web"}

        password = web.get("password")
        if password is not None and str(password) != "":
            cfg.password = str(password)  # 只进内存，不进快照/文件/日志

        chapters = web.get("chapters")
        if isinstance(chapters, list):
            cleaned = [str(item).strip() for item in chapters if str(item).strip()]
            cfg.chapters = cleaned
            snapshot["chapters"] = {"value": list(cleaned), "source": "web"}

        repeat_default = web.get("repeat_default")
        if isinstance(repeat_default, int) and not isinstance(repeat_default, bool):
            cfg.repeat_default = int(repeat_default)
            snapshot["repeat_default"] = {"value": cfg.repeat_default, "source": "web"}

        overrides = web.get("repeat_overrides")
        if isinstance(overrides, dict):
            cleaned_overrides = {str(key).strip(): int(value) for key, value in overrides.items()}
            cfg.repeat_overrides = cleaned_overrides
            snapshot["repeat_overrides"] = {"value": copy.deepcopy(cleaned_overrides), "source": "web"}

        limit = web.get("limit")
        if isinstance(limit, int) and not isinstance(limit, bool):
            cfg.limit = int(limit)
            snapshot["limit"] = {"value": cfg.limit, "source": "web"}

        cfg.validate()  # 复用现有校验（含内存密码），ConfigError 由 start 映射为 INVALID_INPUT
        return cfg, snapshot

    # ---------- 后台执行 ----------
    def _worker(self, cfg: AppConfig, session: RunSession) -> None:
        """唯一 cqa-web-exec 线程：登录 → 解析 → 建计划 → 回填快照 → 执行 → 收口。"""
        try:
            client = self._client_factory(cfg)
            client.login()
            tree = (self._graph_parser(client.get_graph_data_html()) or {}).get("tree")
            completed = self.store.completed_keys()
            plan = self._plan_builder(
                tree, cfg.chapters, cfg.limit, completed,
                repeat_default=cfg.repeat_default,
                repeat_overrides=cfg.repeat_overrides,
                force=False,
            )
            engine = self._chat_engine_factory(cfg)
            executor = self._executor_factory(cfg, client, engine)
            with self._lock:
                session.snapshot["chapter_counts"] = self._chapter_counts(plan)
                session.phase = RunPhase.RUNNING
            for _record in executor.run_plan(plan):
                # 记录由 TaskExecutor / StateStore 落盘；此处不做二次处理，避免改动既有语义。
                pass
            with self._lock:
                session.phase = RunPhase.COMPLETED
        except Exception as exc:
            error_type = getattr(exc, "error_type", None) or type(exc).__name__
            summary = f"{error_type}: {mask(truncate(str(exc), 240))}"
            with self._lock:
                session.phase = RunPhase.FAILED
                session.error_type = str(error_type)
                session.error_summary = summary
            if self.logger is not None and hasattr(self.logger, "error"):
                self.logger.error(f"网页启动执行中止：{mask(truncate(str(exc), 240))}")
        finally:
            # 凭据主动释放：cfg 仅存活于本次线程闭包，进程退出即消失。
            cfg.password = ""
            with self._lock:
                session.finished_at = now_iso()

    @staticmethod
    def _chapter_counts(plan: List[Any]) -> List[Dict[str, Any]]:
        """按大章聚合本次次数与来源，供面板在启动前确认（FR-2.3 / FR-8.1）。"""
        rows: List[Dict[str, Any]] = []
        index: Dict[str, Dict[str, Any]] = {}
        for task in plan:
            key = str(getattr(task, "node_id", "") or getattr(task, "chapter", "") or "")
            if key not in index:
                row = {
                    "chapter": str(getattr(task, "chapter", "") or getattr(task, "node_name", "") or ""),
                    "node_id": str(getattr(task, "node_id", "") or ""),
                    "count": 0,
                    "source": str(getattr(task, "repeat_source", "") or "default"),
                }
                index[key] = row
                rows.append(row)
            row = index[key]
            row["count"] = max(int(row["count"]), int(getattr(task, "repeat_index", 0) or 0))
            if getattr(task, "repeat_source", "") == "override":
                row["source"] = "override"
        return rows
