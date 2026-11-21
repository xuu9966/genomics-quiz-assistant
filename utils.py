"""跨模块纯工具函数（唯一键、脱敏、原子读写、退避、时间）。

本模块不依赖项目内任何其他模块，可被所有层安全导入。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from typing import Any, Optional

# 敏感键：值整体替换为 ***。注意 session_id 由专门规则处理，不能落入此处。
_SENSITIVE_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|session|csrf_token|csrftoken|api_key|apikey|secret|token)"
    r"([\"']?\s*[:=]\s*[\"']?)([^\s\"',;&}\]]+)"
)
_SESSION_ID_RE = re.compile(
    r"(?i)\b(session_id|sessionid)\b([\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9_\-]+)"
)


def normalize_md_path(path: str) -> str:
    """归一化 md 路径，仅用于生成唯一键，不改变实际请求路径。"""
    value = str(path or "").strip().replace("\\", "/")
    while "//" in value:
        value = value.replace("//", "/")
    return value


def make_unique_key(node_id: str, md_path: str) -> str:
    """全链路唯一键（v1 legacy）：节点 id :: 归一化后的文件路径。"""
    return f"{str(node_id or '').strip()}::{normalize_md_path(md_path)}"


def make_task_key(node_id: str, repeat_index: int) -> str:
    """v2 全链路主键：大章节点 id :: 第 k 次（k 从 1 开始）。

    与 v1 旧键双轨并存：新计划全程使用本函数生成唯一键；旧函数保留兼容历史数据与用例。
    """
    return f"{str(node_id or '').strip()}::{int(repeat_index)}"


def chapter_from_path(path: str, fallback: str = "") -> str:
    """取路径顶层目录名作为章节；顶层即为文件名时使用兜底名称。"""
    normalized = normalize_md_path(path)
    parts = [part for part in normalized.split("/") if part]
    if len(parts) >= 2 and not parts[0].lower().endswith(".md"):
        return parts[0]
    return str(fallback or "").strip()


def md5_text(text: str) -> str:
    """题干去重用的 MD5（UTF-8 原文，不做任何预处理）。"""
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


def ensure_dir(path: str) -> None:
    """确保目录存在；空路径视为当前目录。"""
    directory = os.path.abspath(path or ".")
    os.makedirs(directory, exist_ok=True)


def atomic_write_json(path: str, data: Any) -> None:
    """同目录临时文件 + os.replace 原子写入，避免面板读到半截 JSON。"""
    target = os.path.abspath(path)
    ensure_dir(os.path.dirname(target))
    fd, tmp_path = tempfile.mkstemp(prefix=".cqa-tmp-", suffix=".json", dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def read_json(path: str, default: Any = None) -> Any:
    """读取 JSON；文件不存在时返回 default。"""
    if not path or not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def backoff_delay(retry_times: int, base: float, cap: float = 60.0) -> float:
    """第 n 次重试延迟 = min(base * 2^(n-1), cap)，n 从 1 开始。"""
    index = max(1, int(retry_times))
    delay = max(0.0, float(base)) * (2 ** (index - 1))
    return float(min(delay, cap))


def _short_session(value: str) -> str:
    return f"{value[:8]}…" if len(value) > 8 else "***"


def mask(text: Any) -> str:
    """统一日志/导出脱敏：凭据值替换为 ***，session_id 仅留前 8 位。"""
    value = str(text) if text is not None else ""

    def _mask_session(match: re.Match[str]) -> str:
        return match.group(1) + match.group(2) + _short_session(match.group(3))

    def _mask_secret(match: re.Match[str]) -> str:
        return match.group(1) + match.group(2) + "***"

    value = _SESSION_ID_RE.sub(_mask_session, value)
    value = _SENSITIVE_RE.sub(_mask_secret, value)
    return value


def now_iso() -> str:
    """当前本地时间，格式 YYYY-MM-DD HH:MM:SS。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def truncate(text: Any, limit: int = 300) -> str:
    """限制可读摘要长度，避免日志/表格被超长返回体撑破。"""
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + "…"


def as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """宽松数值转换，失败返回 default。"""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
