"""章节自测复习助手 CLI 入口：config / plan / run / bank / serve。

零第三方依赖；run 子命令同时启动回环控制台与后台单线程执行器。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from typing import Any, Dict, List, Optional

import parsers
from answer_engine import PlatformChatAnswerEngine
from bank import QuestionBank
from config import DEFAULT_CONFIG, ConfigError, masked_view, template
from executor import TaskExecutor
from logger import Logger
from models import AppConfig, Task
from platform_client import AuthError, PlatformClient, PlatformError, RequestError
from planner import Planner, build_chapter_plan, parse_limit_argument
from state_store import StateStore
from utils import chapter_from_path, mask, read_json
from webapp import WebApp

DEFAULT_CONFIG_PATH = "config.json"


def build_logger(cfg: AppConfig) -> Logger:
    """按配置目录创建统一日志器。"""
    log_dir = str((cfg.paths or {}).get("log_dir") or "logs")
    return Logger(os.path.join(log_dir, "app.log"))


def build_store(cfg: AppConfig, logger: Any = None) -> StateStore:
    """按配置目录创建状态存储；v1 迁移提示经统一日志器输出。"""
    data_dir = str((cfg.paths or {}).get("data_dir") or "data")
    return StateStore(os.path.join(data_dir, "state.json"),
                      config_masked=cfg.safe_dict(), logger=logger)


def build_client(cfg: AppConfig, logger: Logger) -> PlatformClient:
    """创建平台适配层；request_timeout 与单任务超时联动。"""
    return PlatformClient(cfg, logger=logger, request_timeout=float(max(30, cfg.task_timeout_sec)))


def build_executor(cfg: AppConfig, client: PlatformClient, store: StateStore,
                   bank: QuestionBank, logger: Logger, force: bool = False) -> TaskExecutor:
    engine = PlatformChatAnswerEngine(cfg.model)
    return TaskExecutor(client, engine, store, bank, logger, cfg, force=force)


def cmd_config(args: argparse.Namespace) -> int:
    """交互式生成配置；密码永不写入文件。"""
    path = str(args.config or DEFAULT_CONFIG_PATH)
    existing = read_json(path, None)
    data: Dict[str, Any] = template()
    if isinstance(existing, dict):
        for key, value in existing.items():
            if key in data:
                data[key] = value

    def ask(label: str, current: Any, cast: Any = str) -> Any:
        default = "" if current is None else str(current)
        raw = input(f"{label}（回车保持当前值）[{default}]: ").strip()
        if not raw:
            return current
        return cast(raw)

    data["base_url"] = ask("站点地址", data.get("base_url"))
    data["username"] = ask("用户名", data.get("username"))
    raw_chapters = ask("章节（逗号分隔，留空表示不限）", ",".join(data.get("chapters") or []))
    if isinstance(raw_chapters, str):
        from config import re_split_chapters
        data["chapters"] = [part.strip() for part in re_split_chapters(raw_chapters) if part.strip()]
    data["limit"] = ask("单次限量（大章数，0 表示全量）", data.get("limit", 10), int)
    data["repeat_default"] = ask("每章默认复习次数", data.get("repeat_default", 3), int)
    data["model"] = ask("默认模型", data.get("model", "deepseek-v4-flash"))
    print("按章覆盖次数请在配置文件的 repeat_overrides 中设置（键为章名或大章节点 id）。")

    cfg = AppConfig._from_dict(data)
    cfg.save(path)
    env_name = cfg.password_env
    print(f"配置已保存：{path}")
    print("密码不会写入配置文件；运行时请设置环境变量 " + env_name + "，或按提示无回显输入。")
    print("配置视图（已脱敏）:")
    print(json.dumps(cfg.safe_dict(), ensure_ascii=False, indent=2))
    return 0


def _chapter_outline(plan: List[Task]) -> List[str]:
    """按大章聚合展示行：章名 / 大章节点 id / 本次次数 M / 次数来源（FR-2.3）。"""
    grouped: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for task in plan:
        if task.node_id not in grouped:
            order.append(task.node_id)
            grouped[task.node_id] = {
                "name": task.chapter or task.node_name,
                "count": 0,
                "source": task.repeat_source or "default",
            }
        item = grouped[task.node_id]
        item["count"] = max(int(item["count"]), int(task.repeat_index or 0))
        if task.repeat_source == "override":
            item["source"] = "override"
    lines: List[str] = []
    for node_id in order:
        item = grouped[node_id]
        source_text = "覆盖" if item["source"] == "override" else "默认"
        name = item["name"]
        lines.append(
            f"大章 {name}（节点 id {node_id}）：本次 {item['count']} 次，次数来源 {source_text}。"
        )
    return lines


def _fetch_plan(cfg: AppConfig, client: PlatformClient, store: StateStore, logger: Logger,
                limit: int, force: bool, all_chapters: bool = False) -> List[Task]:
    """登录 → 解析顶层大章 → 生成 v2 计划（网络只发生在 client 适配层）。"""
    client.login()
    tree = parsers.parse_graph_data(client.get_graph_data_html()).get("tree")
    chapters = [] if all_chapters else cfg.chapters
    completed = store.completed_keys()
    plan = build_chapter_plan(
        tree, chapters, limit, completed,
        repeat_default=cfg.repeat_default,
        repeat_overrides=cfg.repeat_overrides,
        force=force,
    )
    for line in _chapter_outline(plan):
        logger.info(line)
    chapter_count = len({task.node_id for task in plan})
    logger.info(
        f"本次纳入 {len(plan)} 个执行单元（{chapter_count} 个大章；"
        f"限量 {limit or '全量'} 章，章节 {chapters or '不限'}）。"
    )
    return plan


def _print_plan(plan: List[Task]) -> None:
    print(f"{'#':>4}  {'大章/第k次（来源）':<24}  {'节点 id':<24}  唯一键")
    for index, task in enumerate(plan, start=1):
        source = "覆盖" if task.repeat_source == "override" else "默认"
        display = f"{task.label}（{source}）"
        print(f"{index:>4}  {display[:24]:<24}  {task.node_id[:24]:<24}  {task.unique_key}")
    print(f"合计 {len(plan)} 个执行单元。")


def cmd_plan(args: argparse.Namespace) -> int:
    """仅规划并打印任务清单，不发起组卷/交卷。"""
    cfg = AppConfig.load(args.config)
    limit = parse_limit_argument(args.limit, args.yes)
    logger = build_logger(cfg)
    store = build_store(cfg, logger)
    client = build_client(cfg, logger)
    plan = _fetch_plan(cfg, client, store, logger, limit, args.force, args.all_chapters)
    _print_plan(plan)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """启动控制台（主线程）与后台执行器（守护线程）。"""
    cfg = AppConfig.load(args.config)
    limit = parse_limit_argument(args.limit, args.yes)
    logger = build_logger(cfg)
    store = build_store(cfg, logger)
    bank = QuestionBank()
    client = build_client(cfg, logger)
    executor = build_executor(cfg, client, store, bank, logger, args.force)
    web = WebApp(cfg, store, bank, logger, executor)

    def worker() -> None:
        try:
            plan = _fetch_plan(cfg, client, store, logger, limit, args.force, args.all_chapters)
            for record in executor.run_plan(plan):
                logger.info(f"任务状态更新：{record.status}，唯一键 {record.unique_key[:60]}")
            if args.collect_history:
                added = executor.collect_conclusions(pages=args.history_pages)
                logger.info(f"评阅总结采集完成，新增题库 {added} 题。")
            logger.info("本轮复习任务已全部处理。")
        except Exception as exc:
            error_type = getattr(exc, "error_type", type(exc).__name__)
            logger.error(f"执行流程中止（{error_type}）：{mask(str(exc))}")

    thread = threading.Thread(target=worker, name="cqa-executor", daemon=True)
    thread.start()
    print("执行线程已启动；关闭浏览器不会中断后台任务。", flush=True)
    try:
        web.start()
    except KeyboardInterrupt:
        print("\n正在停止控制台；后台任务将随进程退出。", flush=True)
    finally:
        web.stop()
    return 0


def cmd_bank(args: argparse.Namespace) -> int:
    """从任务列表 conclusion 汇聚题库并导出（只读接口）。"""
    cfg = AppConfig.load(args.config)
    logger = build_logger(cfg)
    client = build_client(cfg, logger)
    bank = QuestionBank()
    client.login()
    added = 0
    for page in range(1, max(1, args.pages) + 1):
        payload = client.list_tasks(page)
        tasks = payload.get("tasks") or []
        for item in tasks:
            if not isinstance(item, dict) or not item.get("conclusion"):
                continue
            chapter = ""
            try:
                content = json.loads(item.get("content") or "{}")
                if isinstance(content, dict):
                    chapter = str(content.get("chapter") or "")
            except (ValueError, TypeError):
                pass
            added += bank.collect_from_conclusion(item["conclusion"], chapter)
        if page >= int(payload.get("total_pages") or 1):
            break

    from exporters import bank_html, bank_json, bank_markdown
    export_dir = str((cfg.paths or {}).get("export_dir") or "exports")
    os.makedirs(export_dir, exist_ok=True)
    fmt = str(args.format or "html").lower()
    ext = "json" if fmt == "json" else ("md" if fmt in ("md", "markdown") else "html")
    output = str(args.output or os.path.join(export_dir, f"quiz-bank.{ext}"))
    if fmt == "json":
        bank_json(bank.all(), output)
    elif fmt in ("md", "markdown"):
        bank_markdown(bank.all(), output)
    else:
        bank_html(bank.all(), output)
    print(f"题库采集完成：新增 {added} 题，当前共 {len(bank)} 题，已导出到 {output}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """只启动控制台，不启动执行器；可选在面板就绪后打开本机浏览器。"""
    cfg = AppConfig.load(args.config, require_password=False)
    logger = build_logger(cfg)
    store = build_store(cfg, logger)
    bank = QuestionBank()
    web = WebApp(cfg, store, bank, logger, None)

    if getattr(args, "open_browser", False):
        def open_when_ready() -> None:
            """轮询 server 就绪后打开真实端口（端口可能已顺延）；超时静默退出。"""
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                if web.server is not None:
                    try:
                        webbrowser.open(f"http://127.0.0.1:{web.actual_port}/")
                    except Exception:
                        pass  # 自动打开失败不影响面板，用户可手动访问窗口内地址。
                    return
                time.sleep(0.2)

        threading.Thread(target=open_when_ready, name="cqa-open-browser", daemon=True).start()

    try:
        web.start()
    except KeyboardInterrupt:
        pass
    finally:
        web.stop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="章节自测复习助手：本地配置、任务规划、自动复习、题库导出与回环控制台。",
    )
    parser.set_defaults(command="run", config=DEFAULT_CONFIG_PATH, limit="10", yes=False,
                        force=False, all_chapters=False, collect_history=False,
                        history_pages=1, pages=1, format="html", output="")
    sub = parser.add_subparsers(dest="command")

    p_config = sub.add_parser("config", help="交互式配置账号与复习范围（密码不落盘）")
    p_config.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="配置文件路径")

    p_plan = sub.add_parser("plan", help="解析课程目录并打印任务计划（不组卷、不交卷）")
    p_plan.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    p_plan.add_argument("--limit", default="10", help="限量数量；ALL 表示全量（需 --yes）")
    p_plan.add_argument("--yes", action="store_true", help="确认全量执行")
    p_plan.add_argument("--force", action="store_true", help="忽略历史记录")
    p_plan.add_argument("--all-chapters", action="store_true", help="忽略配置中的章节筛选")

    p_run = sub.add_parser("run", help="启动回环控制台并在后台执行复习任务")
    p_run.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    p_run.add_argument("--limit", default="10", help="限量数量；ALL 表示全量（需 --yes）")
    p_run.add_argument("--yes", action="store_true", help="确认全量执行")
    p_run.add_argument("--force", action="store_true", help="忽略历史记录并重新执行")
    p_run.add_argument("--all-chapters", action="store_true", help="忽略配置中的章节筛选")
    p_run.add_argument("--collect-history", action="store_true", help="结束后采集任务列表评阅总结")
    p_run.add_argument("--history-pages", type=int, default=1, help="采集评阅总结的页数")

    p_bank = sub.add_parser("bank", help="汇聚历史评阅总结并导出复习题库")
    p_bank.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    p_bank.add_argument("--pages", type=int, default=1, help="任务列表页数")
    p_bank.add_argument("--format", choices=["html", "markdown", "md", "json"], default="html")
    p_bank.add_argument("--output", default="", help="导出文件路径")

    p_serve = sub.add_parser("serve", help="只启动回环控制台")
    p_serve.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    p_serve.add_argument("--open-browser", action="store_true",
                         help="面板就绪后用默认浏览器打开本机地址（配合双击启动脚本）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(args_list)
    commands = {
        "config": cmd_config,
        "plan": cmd_plan,
        "run": cmd_run,
        "bank": cmd_bank,
        "serve": cmd_serve,
    }
    try:
        return int(commands.get(args.command, cmd_run)(args))
    except (ConfigError, AuthError, PlatformError, parsers.ParseError) as exc:
        print(f"运行失败：{mask(str(exc))}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"参数错误：{mask(str(exc))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
