"""执行记录持久化：原子 JSON、按唯一键 upsert、断点续跑与强制重跑。"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set

from models import ExecutionRecord, RunState, Task, TaskStatus
from utils import atomic_write_json, now_iso, read_json


class StateStore:
    """state.json 的唯一读写入口；面板只读，执行线程写入。"""

    # v2 顶层 schema 标记；v1（无该字段或值 < 2）加载时一次性归档隔离，不迁移旧键。
    SCHEMA_VERSION: int = 2

    def __init__(self, path: str, config_masked: Optional[Dict[str, Any]] = None,
                 logger: Any = None):
        self.path = os.path.abspath(path or "data/state.json")
        self.config_masked: Dict[str, Any] = dict(config_masked or {})
        self.logger = logger
        self._lock = threading.RLock()

    def load(self) -> RunState:
        """读取状态；文件损坏或不存在时返回空状态而非抛堆栈。"""
        with self._lock:
            data: Any = None
            try:
                data = read_json(self.path, None)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                data = None
            empty = RunState(config_masked=dict(self.config_masked), plan=[], records={},
                             updated_at=now_iso())
            if isinstance(data, dict) and self._is_v1(data):
                self._archive_v1()
                return empty
            if not isinstance(data, dict):
                return empty
            plan: List[Task] = []
            for item in data.get("plan") or []:
                if isinstance(item, dict) and item.get("unique_key"):
                    plan.append(Task.from_dict(item))
            records: Dict[str, ExecutionRecord] = {}
            for key, item in (data.get("records") or {}).items():
                if isinstance(item, dict):
                    records[str(key)] = ExecutionRecord.from_dict(item)
            config_masked = data.get("config_masked") or dict(self.config_masked)
            return RunState(
                config_masked=dict(config_masked),
                plan=plan,
                records=records,
                updated_at=str(data.get("updated_at") or now_iso()),
            )

    @classmethod
    def _is_v1(cls, data: Dict[str, Any]) -> bool:
        """无 schema_version 或值小于 2 视为 v1 记录。"""
        try:
            version = int(data.get("schema_version", 1) or 1)
        except (TypeError, ValueError):
            version = 1
        return version < cls.SCHEMA_VERSION

    def _archive_v1(self) -> None:
        """一次性把 v1 state 移到 data/state.v1.<ts>.json，避免旧键被误用为 v2。"""
        backup_name = "归档失败"
        try:
            folder = os.path.dirname(self.path)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = os.path.join(folder, f"state.v1.{stamp}.json")
            suffix = 1
            while os.path.exists(backup):
                backup = os.path.join(folder, f"state.v1.{stamp}_{suffix}.json")
                suffix += 1
            os.replace(self.path, backup)
            backup_name = os.path.basename(backup)
        except OSError:
            # 归档失败也不阻断运行：旧记录仍返回空状态，不参与完成判定。
            pass
        if self.logger is not None and hasattr(self.logger, "info"):
            self.logger.info(f"已从 v1 升级，旧执行记录已归档：{backup_name}")

    def save(self, state: RunState) -> None:
        """原子写入完整状态，顶层带 schema_version=2。"""
        with self._lock:
            payload = state.to_dict()
            payload["schema_version"] = self.SCHEMA_VERSION
            if not self.config_masked and state.config_masked:
                self.config_masked = dict(state.config_masked)
            atomic_write_json(self.path, payload)

    def upsert(self, record: ExecutionRecord) -> None:
        """按唯一键更新记录；不存在则追加，并刷新时间戳。"""
        with self._lock:
            state = self.load()
            state.records[record.unique_key] = record
            state.updated_at = now_iso()
            self.save(state)

    def get(self, unique_key: str) -> Optional[ExecutionRecord]:
        """按唯一键查询记录。"""
        with self._lock:
            return self.load().records.get(str(unique_key or ""))

    def completed_keys(self) -> Set[str]:
        """只有本地 SUCCESS 记录计入已完成；站点 selftestInfo 不作判据。"""
        with self._lock:
            return {key for key, record in self.load().records.items()
                    if record.status == TaskStatus.SUCCESS.value}

    def merge_plan(self, plan: Iterable[Task], force: bool = False) -> RunState:
        """合并计划：保留历史计划顺序，新任务追加；force 时移除成功记录以重新执行。"""
        with self._lock:
            state = self.load()
            incoming = list(plan or [])
            if force:
                state.plan = incoming
                for task in incoming:
                    state.records.pop(task.unique_key, None)
            else:
                existing_keys = {task.unique_key for task in state.plan}
                for task in incoming:
                    if task.unique_key not in existing_keys:
                        state.plan.append(task)
                        existing_keys.add(task.unique_key)
            state.updated_at = now_iso()
            self.save(state)
            return state

    def clear(self, keys: Iterable[str]) -> None:
        """清除指定唯一键的记录（用于重新执行）；计划本身保留。"""
        with self._lock:
            state = self.load()
            for key in keys or []:
                state.records.pop(str(key), None)
            state.updated_at = now_iso()
            self.save(state)
