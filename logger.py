"""本地滚动日志 + 内存尾部缓冲，统一脱敏后输出。"""
from __future__ import annotations

import logging
import os
import threading
from collections import deque
from logging.handlers import RotatingFileHandler
from typing import List

from utils import ensure_dir, mask, now_iso, truncate


class Logger:
    """文件日志（滚动追加）与面板可读取的 tail(n) 缓冲。"""

    def __init__(self, log_path: str = "logs/app.log", tail_size: int = 200,
                 max_bytes: int = 1024 * 1024, backups: int = 3):
        self.log_path = os.path.abspath(log_path or "logs/app.log")
        ensure_dir(os.path.dirname(self.log_path))
        self.tail_buffer: deque = deque(maxlen=max(10, int(tail_size)))
        self._lock = threading.RLock()
        handler = RotatingFileHandler(
            self.log_path, maxBytes=max(1, int(max_bytes)), backupCount=max(0, int(backups)),
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        # 每个实例使用独立 logger 名，避免多次实例化重复追加 handler。
        self._logger = logging.getLogger(f"chapter_quiz_assistant.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.handlers.clear()
        self._logger.addHandler(handler)
        self._logger.propagate = False

    def log(self, level: str, message: str, task_key: str = "") -> None:
        """写一条日志；先脱敏，再同时进入文件与内存尾部。"""
        safe_message = mask(truncate(message, 1000))
        prefix = f"[{mask(task_key)[:60]}] " if task_key else ""
        line = f"[{now_iso()}] [{str(level or 'INFO').upper()}] {prefix}{safe_message}"
        with self._lock:
            self.tail_buffer.append(line)
            self._logger.info(line)

    def info(self, message: str, task_key: str = "") -> None:
        self.log("INFO", message, task_key)

    def warning(self, message: str, task_key: str = "") -> None:
        self.log("WARNING", message, task_key)

    def error(self, message: str, task_key: str = "") -> None:
        self.log("ERROR", message, task_key)

    def tail(self, n: int = 50) -> List[str]:
        """返回最后 n 条日志（最新在末尾）。"""
        count = max(0, int(n))
        with self._lock:
            items = list(self.tail_buffer)
        return items[-count:] if count else []

    @staticmethod
    def mask(text: str) -> str:
        """对外暴露统一脱敏函数，便于其他模块复用。"""
        return mask(text)
