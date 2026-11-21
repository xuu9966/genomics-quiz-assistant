"""平台适配层：全项目唯一网络出口，封装会话/Cookie/CSRF 与全部站点接口。

底层 transport 可注入；真实请求仅通过 urllib 标准库发出。
本模块不会在导入或实例化时发起任何请求，更不会主动调用组卷/交卷接口。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from typing import Any, Callable, Dict, List, Optional, Tuple

import parsers
from config import ConfigError
from models import AppConfig
from utils import backoff_delay, mask, truncate


class PlatformError(Exception):
    """平台请求失败基类；error_type 与 design §7.2 一致。"""

    error_type = "UNKNOWN"


class AuthError(PlatformError):
    error_type = "AUTH"


class RateLimitError(PlatformError):
    error_type = "RATE_LIMIT"


class ServerError(PlatformError):
    error_type = "SERVER"


class NetworkError(PlatformError):
    error_type = "NETWORK"


class GenerateError(PlatformError):
    error_type = "GENERATE"


class SubmitError(PlatformError):
    error_type = "SUBMIT"


class RequestError(PlatformError):
    error_type = "UNKNOWN"


@dataclass
class Response:
    """统一响应结构，便于注入替身。"""

    status_code: int
    text: str = ""
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= int(self.status_code) < 300

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except (ValueError, TypeError) as exc:
            raise RequestError("接口返回的不是合法 JSON。") from exc


@dataclass
class Paper:
    """组卷结果；不含正确答案。"""

    session_id: str = ""
    redirect: str = ""
    md_name: str = ""
    node_id: str = ""
    task_name: str = ""
    duration: int = 0
    rules: List[Any] = field(default_factory=list)
    questions: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    def all_questions(self) -> List[Tuple[str, Dict[str, Any]]]:
        """按 qt01 → qt02 → 其他的顺序展平题目。"""
        ordered_keys = [key for key in ("qt01", "qt02") if key in self.questions]
        ordered_keys.extend(key for key in self.questions if key not in ordered_keys)
        result: List[Tuple[str, Dict[str, Any]]] = []
        for qtype in ordered_keys:
            items = self.questions.get(qtype) or []
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        result.append((qtype, item))
        return result


Transport = Callable[..., Response]


class PlatformClient:
    """唯一知道站点 URL 与会话细节的适配器。"""

    def __init__(self, config: AppConfig, transport: Optional[Transport] = None,
                 logger: Any = None, csrf_token: str = "",
                 request_timeout: float = 120.0):
        self.config = config
        self.logger = logger
        self.cookies = CookieJar()
        self.csrf_token = str(csrf_token or "")
        self.logged_in = False
        self.request_timeout = float(request_timeout or 120.0)
        self._transport = transport
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )
        self._lock = threading.RLock()
        self._last_request_at = 0.0
        self._reauthenticated = False

    # ---------- 内部工具 ----------
    def _log(self, message: str, level: str = "INFO") -> None:
        if self.logger is not None and hasattr(self.logger, "log"):
            self.logger.log(level, f"[平台] {truncate(message, 300)}")

    def _url(self, path: str) -> str:
        base = str(self.config.base_url or "").rstrip("/")
        relative = str(path or "").lstrip("/")
        if "?" in relative:
            raw_path, query = relative.split("?", 1)
            encoded_path = urllib.parse.quote(raw_path, safe="/%")
            encoded_query = urllib.parse.quote(query, safe="=&%+/,:@?~-_*.")
            return f"{base}/{encoded_path}?{encoded_query}"
        return f"{base}/{urllib.parse.quote(relative, safe='/%')}"

    def _throttle(self) -> None:
        """相邻请求间隔 ≥ request_interval_sec，保证全局串行节流。"""
        interval = max(0.0, float(self.config.request_interval_sec or 0.0))
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_request_at = time.monotonic()

    def _default_transport(self, method: str, url: str, headers: Dict[str, str],
                           data: Optional[bytes], timeout: float) -> Response:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return Response(
                    status_code=int(response.status),
                    text=response.read().decode(charset, "replace"),
                    url=response.geturl(),
                    headers=dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = str(exc)
            return Response(int(exc.code), body, exc.geturl(), dict((exc.headers.items() if exc.headers else [])))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NetworkError(f"网络请求失败：{mask(str(exc))}") from exc

    def _send(self, method: str, path: str, *, json_body: Any = None,
              form: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None,
              timeout: Optional[float] = None) -> Response:
        request_headers: Dict[str, str] = dict(headers or {})
        request_headers.setdefault("User-Agent", "ChapterQuizAssistant/1.0 (local use)")
        data: Optional[bytes] = None
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json; charset=utf-8"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        self._throttle()
        transport = self._transport or self._default_transport
        return transport(method, self._url(path), request_headers, data,
                         float(timeout or self.request_timeout))

    @staticmethod
    def _is_login_page(response: Response) -> bool:
        url = urllib.parse.unquote(str(response.url or ""))
        if "/用户管理/登录" in url:
            return True
        head = str(response.text or "")[:1200]
        return response.status_code in (401, 403) and "登录" in head and "<form" in head

    def _sync_csrf(self) -> None:
        for cookie in self.cookies:
            if cookie.name == "csrf_token" and cookie.value:
                self.csrf_token = cookie.value
                return

    # ---------- 认证与通用请求 ----------
    def login(self) -> None:
        """登录并保存 Cookie；失败抛 AuthError/ConfigError。"""
        if not str(self.config.base_url or "").strip():
            raise ConfigError("未配置站点地址 base_url。")
        if not str(self.config.username or "").strip():
            raise ConfigError("未配置用户名 username。")
        if not str(self.config.password or ""):
            raise ConfigError("未提供密码，无法登录。")
        form = {"username": self.config.username, "password": self.config.password}
        retry_times = max(0, int(self.config.retry_times))
        last_message = "登录失败"
        for attempt in range(retry_times + 1):
            response = self._send("POST", "/用户管理/登录", form=form)
            if response.status_code == 429 or 500 <= response.status_code:
                if attempt < retry_times:
                    delay = backoff_delay(attempt + 1, self.config.retry_backoff_sec)
                    self._log(f"登录受限，{delay:.1f} 秒后重试（{attempt + 1}/{retry_times}）", "WARNING")
                    time.sleep(delay)
                    continue
                if response.status_code == 429:
                    raise RateLimitError("登录请求被限流，重试后仍未成功。")
                raise ServerError(f"登录服务异常：HTTP {response.status_code}")
            if self._is_login_page(response):
                raise AuthError("用户名或密码错误，或登录被平台拒绝。")
            if response.status_code >= 400:
                raise AuthError(f"登录失败：HTTP {response.status_code}。")
            self._sync_csrf()
            self.logged_in = True
            self._reauthenticated = False
            self._log("登录成功，会话与 CSRF 令牌已在内存中建立。")
            return
        raise AuthError(last_message)

    def request_with_retry(self, method: str, path: str, **kwargs: Any) -> Response:
        """串行请求 + 429/5xx 退避重试 + 401 重登一次。"""
        with self._lock:
            retry_times = max(0, int(self.config.retry_times))
            for attempt in range(retry_times + 1):
                response = self._send(method, path, **kwargs)
                if response.status_code == 401 or self._is_login_page(response):
                    if not self._reauthenticated:
                        self._reauthenticated = True
                        self._log("会话失效，重新登录后重放请求。", "WARNING")
                        self.login()
                        continue
                    raise AuthError("重新登录后仍被要求登录。")
                if response.status_code == 429 or 500 <= response.status_code:
                    if attempt < retry_times:
                        delay = backoff_delay(attempt + 1, self.config.retry_backoff_sec)
                        self._log(
                            f"HTTP {response.status_code}，退避 {delay:.1f} 秒后重试（{attempt + 1}/{retry_times}）",
                            "WARNING",
                        )
                        time.sleep(delay)
                        continue
                    if response.status_code == 429:
                        raise RateLimitError(f"HTTP 429 Too Many Requests，重试 {retry_times} 次后仍受限。")
                    raise ServerError(f"HTTP {response.status_code}，重试 {retry_times} 次后服务仍不可用。")
                if response.status_code >= 400:
                    raise RequestError(f"请求失败：HTTP {response.status_code}，{mask(truncate(response.text, 120))}")
                self._reauthenticated = False
                return response
            raise ServerError("请求重试次数耗尽。")

    # ---------- 只读接口 ----------
    def get_graph_data_html(self) -> str:
        """资料页直接请求 /资料/，避免 308 重定向。"""
        return self.request_with_retry("GET", "/资料/").text

    def get_question_types(self) -> Dict[str, str]:
        payload = self.request_with_retry("GET", "/资料/api/question_types").json()
        return parsers.parse_question_types(payload)

    def get_default_model(self) -> str:
        try:
            payload = self.request_with_retry("GET", "/api/user/default_model").json()
            name = payload.get("name") if isinstance(payload, dict) else None
            if name:
                return str(name)
        except PlatformError:
            pass
        return str(self.config.model or "deepseek-v4-flash")

    def get_result_html(self, session_id: str) -> str:
        safe_id = urllib.parse.quote(str(session_id or ""), safe="")
        return self.request_with_retry("GET", f"/功能/学生/学习任务/查看/{safe_id}").text

    def list_tasks(self, page: int = 1) -> Dict[str, Any]:
        query = urllib.parse.urlencode({"page": page, "search": "", "task_type_id": ""})
        payload = self.request_with_retry("GET", f"/功能/学生/学习任务/数据?{query}").json()
        return payload if isinstance(payload, dict) else {"tasks": [], "total": 0, "total_pages": 1}

    # ---------- 写接口（仅由执行器显式调用） ----------
    def generate_paper(self, node_id: str, md_path: str, paper: Any = None) -> Paper:
        """组卷。paper 可为 dict 配置或 None（使用全局配置）。"""
        # 不修改 self.config：执行期间配置对象被多处只读共享，避免竞态。
        if isinstance(paper, list):
            paper_config = paper
        elif isinstance(paper, dict) and ("single_count" in paper or "multi_count" in paper):
            single = int(paper.get("single_count", self.config.paper.get("single_count", 5)))
            multi = int(paper.get("multi_count", self.config.paper.get("multi_count", 5)))
            score = int(paper.get("score_per", self.config.paper.get("score_per", 10)))
            paper_config = [
                {"qtype_id": "qt01", "count": single, "score": score},
                {"qtype_id": "qt02", "count": multi, "score": score},
            ]
        else:
            paper_config = self.config.paper_items()
        duration = int(paper.get("duration", self.config.paper.get("duration", 0))) if isinstance(paper, dict) else int(self.config.paper.get("duration", 0))
        payload = {
            "node_id": str(node_id or ""),
            "md_path": str(md_path or ""),  # 原样请求，不归一化、不替换分隔符
            "config": paper_config,
            "duration": duration,
        }
        response = self.request_with_retry("POST", "/功能/api/selftest/generate", json_body=payload)
        data = response.json()
        if not isinstance(data, dict):
            raise GenerateError("组卷接口返回格式异常。")
        if data.get("success") is False or not data.get("session_id"):
            raise GenerateError(f"组卷失败：{mask(truncate(str(data.get('error') or data.get('message') or '未知原因'), 200))}")
        questions = data.get("questions") or {}
        if not isinstance(questions, dict):
            questions = {}
        # 平台把每个题型包成 {"count":..,"questions":[...]} 的对象（交卷分桶结构），
        # 也兼容老式的「题型 → 数组」；归一化逻辑见 parsers.normalize_questions。
        normalized = parsers.normalize_questions(questions)
        if parsers.count_questions(normalized) <= 0:
            # 组卷成功但一道题都没有：必须显式失败，绝不允许交空卷刷出 0 分还记 success。
            raise GenerateError("组卷返回 0 道题（题型/数量可能与平台不匹配），已中止，未交卷。")
        return Paper(
            session_id=str(data.get("session_id") or ""),
            redirect=str(data.get("redirect") or ""),
            md_name=str(data.get("md_name") or ""),
            node_id=str(data.get("node_id") or node_id or ""),
            task_name=str(data.get("task_name") or ""),
            duration=int(data.get("duration") or 0),
            rules=list(data.get("rules") or []),
            questions=normalized,
            raw=data,
        )

    def submit_paper(self, session_id: str, payload: Dict[str, Any]) -> None:
        """交卷。payload 由 executor 按 §7.7 构造。"""
        safe_id = urllib.parse.quote(str(session_id or ""), safe="")
        response = self.request_with_retry(
            "POST", f"/功能/api/selftest/submit/{safe_id}", json_body=payload
        )
        try:
            data = response.json()
        except RequestError:
            return  # 部分部署返回空响应体，视为提交成功
        if isinstance(data, dict) and (data.get("success") is False or data.get("error")):
            raise SubmitError(f"交卷失败：{mask(truncate(str(data.get('error') or data.get('message')), 200))}")

    def chat(self, message: str, model: Optional[str] = None) -> str:
        """调用平台问答；仅此接口携带 X-CSRF-Token。"""
        payload: Dict[str, Any] = {
            "model": str(model or self.config.model or "deepseek-v4-flash"),
            "message": str(message or ""),
            "session_id": "",
            "stream": False,
            "rag_mode": "",
            "api_key": "",
            "theme": "",
            "multi_turn": False,
            "web_search": False,
            "inject_knowledge_frame": True,
            "embedded_auto": True,
        }
        headers = {"X-CSRF-Token": self.csrf_token} if self.csrf_token else {}
        response = self.request_with_retry("POST", "/api/chat", json_body=payload, headers=headers)
        data = response.json()
        if not isinstance(data, dict):
            raise RequestError("AI 接口返回格式异常。")
        if data.get("error"):
            raise RequestError(f"AI 接口报错：{mask(truncate(str(data.get('error')), 200))}")
        if "response" not in data:
            raise RequestError("AI 接口未返回 response 字段。")
        return str(data["response"])
