"""配置读写、环境变量覆盖、校验与无回显密码输入。

本模块只处理 dict，不导入 models，避免与 models.AppConfig 形成循环依赖。
"""
from __future__ import annotations

import copy
import getpass
import os
from typing import Any, Dict

from utils import atomic_write_json, ensure_dir, mask, read_json


class ConfigError(Exception):
    """配置缺失、损坏或不合法；错误文案面向用户，不输出堆栈。"""


# 每章重复次数上限：核验 selftestInfo.limit=99，配置侧用该值兜底校验，不预取运行时值。
REPEAT_LIMIT: int = 99


DEFAULT_CONFIG: Dict[str, Any] = {
    "base_url": "",
    "username": "",
    "password": "",  # 永不落盘，仅在内存/环境变量中存在
    "password_env": "CQA_PASSWORD",
    "chapters": [],
    "limit": 10,
    "repeat_default": 3,
    "repeat_overrides": {},
    "model": "deepseek-v4-flash",
    "paper": {
        "single_count": 5,
        "multi_count": 5,
        "score_per": 10,
        "duration": 0,
    },
    "request_interval_sec": 2.0,
    "retry_times": 3,
    "retry_backoff_sec": 2.0,
    "task_timeout_sec": 180,
    "score_poll_interval_sec": 5,
    "score_poll_timeout_sec": 90,
    "web": {"host": "127.0.0.1", "port": 8765, "poll_sec": 3, "status_color": False},
    "paths": {"data_dir": "data", "log_dir": "logs", "export_dir": "exports"},
}

_ENV_MAP: Dict[str, str] = {
    "CQA_BASE_URL": "base_url",
    "CQA_USERNAME": "username",
    "CQA_PASSWORD": "password",
    "CQA_PASSWORD_ENV": "password_env",
    "CQA_CHAPTERS": "chapters",
    "CQA_LIMIT": "limit",
    "CQA_REPEAT_DEFAULT": "repeat_default",
    "CQA_MODEL": "model",
    "CQA_REQUEST_INTERVAL_SEC": "request_interval_sec",
    "CQA_RETRY_TIMES": "retry_times",
    "CQA_RETRY_BACKOFF_SEC": "retry_backoff_sec",
    "CQA_TASK_TIMEOUT_SEC": "task_timeout_sec",
    "CQA_SCORE_POLL_INTERVAL_SEC": "score_poll_interval_sec",
    "CQA_SCORE_POLL_TIMEOUT_SEC": "score_poll_timeout_sec",
    "CQA_WEB_PORT": "web.port",
    "CQA_WEB_POLL_SEC": "web.poll_sec",
    "CQA_DATA_DIR": "paths.data_dir",
    "CQA_LOG_DIR": "paths.log_dir",
    "CQA_EXPORT_DIR": "paths.export_dir",
}


def template() -> Dict[str, Any]:
    """返回无真实凭据的配置模板字典。"""
    value = copy.deepcopy(DEFAULT_CONFIG)
    value.pop("password", None)
    return value


def _deep_update(target: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value
    return target


def load_config(path: str) -> Dict[str, Any]:
    """读取配置并与默认值合并；损坏时给可读错误。"""
    if not path or not os.path.exists(path):
        raise ConfigError(f"配置文件不存在：{mask(path or '(空)')}，请先执行 config 子命令初始化。")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            user_data = json_load(handle.read(), path)
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件：{mask(str(exc))}") from exc
    if not isinstance(user_data, dict):
        raise ConfigError("配置文件顶层必须是 JSON 对象。")
    merged = copy.deepcopy(DEFAULT_CONFIG)
    return _deep_update(merged, user_data)


def json_load(text: str, source: str = "配置") -> Any:
    """json.loads 包装，统一可读错误。"""
    import json

    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{source}格式错误（不是合法 JSON）：{mask(str(exc))}") from exc


def save_config(path: str, data: Dict[str, Any]) -> None:
    """保存配置；强制剔除 password 字段，确保凭据不落盘。"""
    value = copy.deepcopy(dict(data or {}))
    value.pop("password", None)
    for key in list(value.keys()):
        if "password" in str(key).lower() and key != "password_env":
            value.pop(key, None)
    ensure_dir(os.path.dirname(os.path.abspath(path or "config.json")))
    atomic_write_json(path or "config.json", value)


def _set_path(data: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def apply_env_overrides(data: Dict[str, Any]) -> Dict[str, Any]:
    """用环境变量覆盖配置；仅非空变量生效，返回同一 dict。"""
    for env_name, key in _ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw is None or str(raw).strip() == "":
            continue
        text = str(raw).strip()
        if key == "chapters":
            data[key] = [part.strip() for part in re_split_chapters(text) if part.strip()]
        elif key in ("limit", "repeat_default", "retry_times", "task_timeout_sec", "web.port",
                     "web.poll_sec", "score_poll_interval_sec", "score_poll_timeout_sec"):
            _set_path(data, key, int(text))
        elif key in ("request_interval_sec", "retry_backoff_sec"):
            _set_path(data, key, float(text))
        else:
            _set_path(data, key, text)
    return data


def re_split_chapters(text: str) -> list:
    """章节列表支持英文逗号、中文逗号、换行分隔。"""
    import re

    return re.split(r"[,，\n;；]+", str(text or ""))


def prompt_password(env_name: str = "CQA_PASSWORD") -> str:
    """优先读环境变量，否则 getpass 无回显输入；只返回内存字符串。"""
    name = str(env_name or "CQA_PASSWORD").strip() or "CQA_PASSWORD"
    value = os.environ.get(name, "")
    if str(value).strip():
        return str(value)
    return getpass.getpass("平台密码（不回显，仅保存在内存中）: ")


def validate_config(data: Dict[str, Any], require_password: bool = True,
                    require_username: bool = True) -> None:
    """结构校验；任何问题抛 ConfigError，附带具体可读原因。

    require_username 用于网页启动（serve）模式：学号改由网页表单提交，
    此时 config.json 的 username 允许留空，故该模式下传 False。
    """
    base_url = str(data.get("base_url") or "").strip()
    if not base_url or not base_url.startswith(("http://", "https://")):
        raise ConfigError("站点地址 base_url 必填，且需以 http:// 或 https:// 开头。")
    username = str(data.get("username") or "").strip()
    if require_username and not username:
        raise ConfigError("用户名 username 必填。")
    if require_password and not str(data.get("password") or ""):
        env_name = str(data.get("password_env") or "CQA_PASSWORD")
        raise ConfigError(f"未提供密码：请设置环境变量 {env_name} 或按提示输入。")

    chapters = data.get("chapters")
    if chapters is None:
        data["chapters"] = []
    elif not isinstance(chapters, list) or any(not isinstance(item, str) for item in chapters):
        raise ConfigError("chapters 必须是字符串数组（如 [\"2_测序\", \"3_组装\"]）。")

    limit = data.get("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ConfigError("limit 必须是非负整数；0 表示不限制数量。")

    # v2 增量：缺省值兜底保证旧配置（无这两个键）仍可加载。
    repeat_default = data.get("repeat_default", 3)
    if (isinstance(repeat_default, bool) or not isinstance(repeat_default, int)
            or not (1 <= repeat_default <= REPEAT_LIMIT)):
        raise ConfigError(f"repeat_default 必须是 1~{REPEAT_LIMIT} 的正整数。")
    overrides = data.get("repeat_overrides", {})
    if not isinstance(overrides, dict):
        raise ConfigError("repeat_overrides 必须是对象（键为章名或大章节点 id，值为正整数次数）。")
    for name, value in overrides.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("repeat_overrides 的键必须是非空字符串（章名或大章节点 id）。")
        if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= REPEAT_LIMIT):
            raise ConfigError(f"repeat_overrides[{name}] 必须是 1~{REPEAT_LIMIT} 的正整数。")

    model = str(data.get("model") or "").strip()
    if not model:
        raise ConfigError("model 不能为空。")

    paper = data.get("paper")
    if not isinstance(paper, dict):
        raise ConfigError("paper 必须是对象，含 single_count/multi_count/score_per/duration。")
    for key in ("single_count", "multi_count", "score_per", "duration"):
        val = paper.get(key, 0)
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise ConfigError(f"paper.{key} 必须是非负整数。")
    if paper.get("single_count", 0) + paper.get("multi_count", 0) < 10:
        raise ConfigError("组卷总题数不得少于 10 题，请调整 paper.single_count / multi_count。")
    if (paper.get("single_count", 0) + paper.get("multi_count", 0)) * paper.get("score_per", 0) != 100:
        raise ConfigError("组卷总分必须等于 100 分，请调整 paper.score_per 或题数。")

    for key in ("request_interval_sec", "retry_backoff_sec"):
        val = data.get(key, 0)
        if isinstance(val, bool) or not isinstance(val, (int, float)) or val < 0:
            raise ConfigError(f"{key} 必须是非负数。")
    for key in ("retry_times", "task_timeout_sec", "score_poll_interval_sec", "score_poll_timeout_sec"):
        val = data.get(key, 0)
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            raise ConfigError(f"{key} 必须是正整数。")
    if data.get("score_poll_interval_sec", 1) > data.get("score_poll_timeout_sec", 1):
        raise ConfigError("出分轮询间隔不得大于出分超时时间。")

    web = data.get("web")
    if not isinstance(web, dict):
        raise ConfigError("web 必须是对象，含 host/port/poll_sec/status_color。")
    port = web.get("port", 8765)
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ConfigError("web.port 必须是 1~65535 的整数。")
    poll_sec = web.get("poll_sec", 3)
    if isinstance(poll_sec, bool) or not isinstance(poll_sec, int) or not (1 <= poll_sec <= 10):
        raise ConfigError("web.poll_sec 必须是 1~10 的整数（刷新延迟 ≤10 秒）。")
    if not isinstance(web.get("status_color", False), bool):
        raise ConfigError("web.status_color 必须是布尔值，默认 false。")

    paths = data.get("paths")
    if not isinstance(paths, dict):
        raise ConfigError("paths 必须是对象，含 data_dir/log_dir/export_dir。")
    for key in ("data_dir", "log_dir", "export_dir"):
        if not isinstance(paths.get(key), str) or not paths.get(key, "").strip():
            raise ConfigError(f"paths.{key} 必须是非空字符串。")


_SENSITIVE_KEYS = {"password", "password_env_value", "api_key", "csrf_token", "session", "secret", "token"}


def masked_view(data: Any) -> Any:
    """递归脱敏视图，供配置展示、/api/state 与导出复用。"""
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if str(key).lower() in _SENSITIVE_KEYS:
                if value in (None, "", False):
                    result[key] = value
                else:
                    result[key] = "***"
            else:
                result[key] = masked_view(value)
        return result
    if isinstance(data, list):
        return [masked_view(item) for item in data]
    if isinstance(data, str):
        return mask(data)
    return data
