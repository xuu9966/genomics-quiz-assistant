"""回环 Web 控制台：静态资源 + 6 个 API（含 v3 网页启动）；仅监听 127.0.0.1。"""
from __future__ import annotations

import io
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import exporters
from models import Task, TaskStatus
from runtime import RunManager
from utils import mask, truncate


class LoopbackHTTPServer(ThreadingHTTPServer):
    """回环面板服务器：关闭 SO_REUSEADDR，避免端口被占用时仍"绑定成功"。

    Windows 下套接字默认复用地址，即便端口已被其他进程监听也能 bind 成功，
    结果是我们的服务看似启动、实际请求被旧进程接走（页面显示成别人的应用）。
    置为 False 后，端口被占用会正确抛 OSError，从而走 start() 的顺延逻辑。
    """

    allow_reuse_address = False


class WebApp:
    """Web 服务容器；start() 阻塞服务，执行器由 main 在后台线程启动。"""

    def __init__(self, config: Any, store: Any, bank: Any, logger: Any,
                 executor: Any = None, static_dir: Optional[str] = None,
                 run_manager: Any = None):
        self.config = config
        self.store = store
        self.bank = bank
        self.logger = logger
        self.executor = executor
        self.static_dir = os.path.abspath(static_dir or os.path.join(os.path.dirname(__file__), "static"))
        self.server: Optional[ThreadingHTTPServer] = None
        self.actual_port: int = int((config.web or {}).get("port", 8765))
        # None 时用现有 config/store/bank/logger 自构造，cmd_serve/cmd_run 零改动即获得启动能力。
        self.run_manager = run_manager if run_manager is not None else RunManager(config, store, bank, logger)

    def _warn_loopback(self, configured_host: str) -> None:
        """Q7 加固：配置被改成非回环地址时告警，并强制回到 127.0.0.1。"""
        message = (
            f"检测到 web.host={mask(configured_host)}，已强制使用回环地址 127.0.0.1"
            "（本机面板禁止对外监听）。"
        )
        print(f"[告警] {message}", flush=True)
        if self.logger is not None:
            for level in ("warning", "error", "info"):
                write = getattr(self.logger, level, None)
                if callable(write):
                    write(message)
                    break

    def _port_occupied(self, host: str, port: int) -> bool:
        """探测端口是否已有程序在监听。

        用 connect_ex 主动连接：连得上说明有人占用（不该复用该端口）；
        连不上（拒绝/超时）视为空闲，可安全 bind。
        """
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.3)
        try:
            return probe.connect_ex((host, port)) == 0
        finally:
            probe.close()

    def start(self, block: bool = True) -> ThreadingHTTPServer:
        """绑定回环地址；端口占用时向后顺延最多 20 个。host 非回环时强制回环并告警。"""
        web = dict(self.config.web or {})
        configured_host = str(web.get("host") or "127.0.0.1").strip()
        if configured_host not in ("127.0.0.1", "localhost", "::1"):
            self._warn_loopback(configured_host)
            web["host"] = "127.0.0.1"
            # 只改内存中的配置对象，绝不写回 config.json。
            if isinstance(getattr(self.config, "web", None), dict):
                self.config.web["host"] = "127.0.0.1"
        host = "127.0.0.1"  # 硬编码回环：任何配置都不能放宽监听范围。
        base_port = int(web.get("port", 8765))
        handler = make_handler(self)
        last_error: Optional[OSError] = None
        for port in range(base_port, base_port + 21):
            if self._port_occupied(host, port):
                # 端口已被其他程序监听：直接跳过，避免 SO_REUSEADDR 劫持后页面串台。
                continue
            try:
                self.server = LoopbackHTTPServer((host, port), handler)
                self.actual_port = port
                break
            except OSError as exc:
                last_error = exc
                continue
        if self.server is None:
            raise RuntimeError(f"回环端口 {base_port}~{base_port + 20} 均被占用：{mask(str(last_error or ''))}")
        url = f"http://127.0.0.1:{self.actual_port}/"
        print(f"复习控制台已启动：{url}（仅本机可访问）", flush=True)
        if self.logger:
            self.logger.info(f"复习控制台已启动：{url}")
        if block:
            self.server.serve_forever(poll_interval=0.2)
        return self.server

    def stop(self) -> None:
        """停止服务；不影响已在后台运行的执行线程。"""
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    # ---------- API ----------
    def api_state(self) -> Dict[str, Any]:
        """一次返回配置脱敏视图 + 完整计划 + 记录 + 日志尾部 + 内存运行态。"""
        state = self.store.load()
        payload = state.to_dict()
        web = dict(self.config.web or {})
        payload["logs"] = self.logger.tail(50) if self.logger else []
        payload["poll_sec"] = int(web.get("poll_sec", 3))
        payload["status_color"] = bool(web.get("status_color", False))
        # 只加一个键；runtime 由 RunSession.to_dict() 白名单产出，绝无 password。
        payload["runtime"] = self.run_manager.snapshot()
        return payload

    def api_start(self, form: Dict[str, Any]) -> Dict[str, Any]:
        """网页启动入口；一行委托 RunManager，不记录请求体。"""
        return self.run_manager.start(form)

    def api_records(self, filters: Dict[str, str]) -> Dict[str, Any]:
        """记录视图：计划 × 记录合并，支持搜索/状态/章节筛选。"""
        keyword = str(filters.get("keyword") or filters.get("search") or "").strip().lower()
        status_filter = str(filters.get("status") or "").strip()
        chapter_filter = str(filters.get("chapter") or "").strip()
        state = self.store.load()
        rows: List[Dict[str, Any]] = []
        tasks = {task.unique_key: task for task in state.plan}
        for key, task in tasks.items():
            record = state.records.get(key)
            row = task.to_dict()
            if record:
                row.update(record.to_dict())
            else:
                row.update({"status": TaskStatus.PENDING.value, "session_id": "", "score": None,
                            "error_type": "", "error_summary": "", "attempts": 0,
                            "started_at": "", "finished_at": ""})
            if status_filter and row.get("status") != status_filter:
                continue
            if chapter_filter and chapter_filter not in str(row.get("chapter") or ""):
                continue
            if keyword and keyword not in " ".join([
                str(row.get("file_name") or ""), str(row.get("chapter") or ""),
                str(row.get("error_summary") or ""), str(row.get("node_name") or ""),
                task.label, str(row.get("repeat_index") or ""),
                str(row.get("repeat_source") or ""),
            ]).lower():
                continue
            rows.append(row)
        return {"records": rows, "total": len(rows)}

    def api_questions(self, filters: Dict[str, str]) -> Dict[str, Any]:
        questions = self.bank.search(
            keyword=str(filters.get("keyword") or filters.get("search") or ""),
            qtype=str(filters.get("qtype") or ""),
            chapter=str(filters.get("chapter") or ""),
        )
        return {"questions": [question.to_dict() for question in questions], "total": len(questions)}

    def api_export(self, fmt: str) -> bytes:
        """按设计签名返回导出字节；不产生任何真实副作用网络请求。"""
        fmt = str(fmt or "html").lower()
        if fmt == "csv":
            data = self.api_records({})["records"]
            buffer = io.StringIO()
            exporters.records_csv(data, buffer)
            return buffer.getvalue().encode("utf-8-sig")
        return exporters.bank_bytes(fmt, self.bank.all())

    def api_control(self, action: str, keys: List[str]) -> Dict[str, Any]:
        """重新执行/清除记录；只登记键，真正执行仍由单线程执行器完成。"""
        cleaned = [str(key) for key in (keys or []) if str(key or "").strip()]
        if action in ("force_rerun", "rerun", "clear"):
            self.store.clear(cleaned)
            queued = self.executor.queue_rerun(cleaned) if self.executor else 0
            return {
                "ok": True,
                "keys": cleaned,
                "queued": queued,
                "message": f"已登记 {len(cleaned)} 项重新执行；正在运行的任务完成后依次处理。",
            }
        return {"ok": False, "error": "未知操作"}


def export_filename(fmt: str) -> str:
    """导出建议文件名（供 Web 响应头使用）。"""
    fmt = str(fmt or "html").lower()
    if fmt == "csv":
        return "quiz-records.csv"
    extension = "json" if fmt == "json" else ("md" if fmt in ("md", "markdown") else "html")
    return f"quiz-bank.{extension}"


def export_mime(fmt: str) -> str:
    """导出 MIME 类型（供 Web 响应头使用）。"""
    fmt = str(fmt or "html").lower()
    if fmt == "csv":
        return "text/csv; charset=utf-8"
    if fmt == "json":
        return "application/json; charset=utf-8"
    return "text/plain; charset=utf-8"


def make_handler(app: WebApp):
    """创建绑定到具体 WebApp 实例的请求处理器。"""

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "ChapterQuizAssistant/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            # 请求日志不写凭据，交给统一 Logger 或不输出。
            return

        def _send(self, status: int, body: bytes, mime: str,
                  extra_headers: Optional[Dict[str, str]] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_json(self, data: Any, status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _send_error(self, message: str, status: int = 400) -> None:
            self._send_json({"ok": False, "error": mask(truncate(message, 200))}, status)

        def _static(self, filename: str) -> None:
            safe_name = os.path.basename(filename or "index.html")
            if safe_name not in ("index.html", "app.css", "app.js"):
                self._send_error("资源不存在", 404)
                return
            path = os.path.join(app.static_dir, safe_name)
            if not os.path.exists(path):
                self._send_error("资源不存在", 404)
                return
            with open(path, "rb") as handle:
                body = handle.read()
            mime = {
                "index.html": "text/html; charset=utf-8",
                "app.css": "text/css; charset=utf-8",
                "app.js": "application/javascript; charset=utf-8",
            }[safe_name]
            self._send(200, body, mime)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            query = {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
            try:
                if path in ("/", "/index.html", "/static/index.html"):
                    self._static("index.html")
                elif path in ("/app.css", "/static/app.css"):
                    self._static("app.css")
                elif path in ("/app.js", "/static/app.js"):
                    self._static("app.js")
                elif path == "/api/state":
                    self._send_json(app.api_state())
                elif path == "/api/records":
                    self._send_json(app.api_records(query))
                elif path == "/api/questions":
                    self._send_json(app.api_questions(query))
                elif path == "/api/export":
                    fmt = query.get("fmt", "html")
                    self._send(200, app.api_export(fmt), export_mime(fmt), {
                        "Content-Disposition": f'attachment; filename="{export_filename(fmt)}"'
                    })
                else:
                    self._send_error("接口不存在", 404)
            except Exception as exc:
                if app.logger:
                    app.logger.error(f"Web GET 失败：{mask(str(exc))}")
                self._send_error(f"请求处理失败：{type(exc).__name__}", 500)

        def _same_origin(self) -> bool:
            """轻量同源校验：无 Origin（脚本/测试直调）放行，其余仅回环来源放行。"""
            origin = str(self.headers.get("Origin") or "").strip()
            if not origin:
                return True
            try:
                parsed = urlparse(origin)
                host = str(parsed.hostname or "").lower()
                return parsed.scheme in ("http", "https") and host in ("127.0.0.1", "localhost")
            except ValueError:
                return False

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            try:
                # 启动接口先做同源校验；任何分支都不记录请求体（密码只在请求体中出现）。
                if path == "/api/start" and not self._same_origin():
                    self._send_json({
                        "ok": False, "code": "FORBIDDEN", "message": "仅允许本机面板提交。",
                    }, 403)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                if path == "/api/start":
                    self._send_json(app.api_start(payload if isinstance(payload, dict) else {}))
                elif path == "/api/control":
                    action = str((payload or {}).get("action") or "")
                    keys = (payload or {}).get("keys") or []
                    if isinstance(keys, str):
                        keys = [keys]
                    self._send_json(app.api_control(action, keys))
                else:
                    self._send_error("接口不存在", 404)
            except (ValueError, TypeError) as exc:
                self._send_error(f"请求体不是合法 JSON：{mask(str(exc))}")
            except Exception as exc:
                if app.logger:
                    app.logger.error(f"Web POST 失败：{mask(str(exc))}")
                self._send_error(f"请求处理失败：{type(exc).__name__}", 500)

    return DashboardHandler
