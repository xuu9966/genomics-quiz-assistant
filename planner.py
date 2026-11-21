"""任务规划：整棵树 → 任务全集、章节筛选、默认限量与强制重跑。"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import parsers
from models import Task
from utils import chapter_from_path, make_task_key, normalize_md_path


def match_chapter(md_path: str, node: Dict[str, Any], whitelist: Iterable[str]) -> bool:
    """章节筛选：同时支持数字编号（2 / 2_测序）与名称前缀（自主学习）。"""
    filters = [str(item).strip() for item in (whitelist or []) if str(item).strip()]
    if not filters:
        return True
    node_name = str((node or {}).get("name") or "")
    chapter = chapter_from_path(md_path, fallback=node_name)
    chapter_number = re.match(r"^\s*(\d+)", chapter)
    node_number = re.match(r"^\s*(\d+)", node_name)

    for item in filters:
        if chapter == item or chapter.startswith(item):
            return True
        if node_name == item or node_name.startswith(item):
            return True
        filter_number = re.match(r"^\s*(\d+)", item)
        if filter_number and chapter_number and filter_number.group(1) == chapter_number.group(1):
            return True
        if filter_number and node_number and filter_number.group(1) == node_number.group(1):
            return True
    return False


def match_chapter_node(chapter_node: Dict[str, Any], whitelist: Iterable[str]) -> bool:
    """大章筛选：md_path 传空，使匹配落到节点名称（沿用数字编号 + 名称前缀规则）。"""
    return match_chapter("", chapter_node, whitelist)


def _chapter_repeat(node: Dict[str, Any], overrides: Dict[str, int],
                    repeat_default: int) -> Tuple[int, str]:
    """解析某大章的重复次数：节点 id 优先、章名次之，均未命中走默认值。"""
    node_id = str((node or {}).get("id", "") or "").strip()
    name = str((node or {}).get("name", "") or "")
    if node_id in overrides:
        return int(overrides[node_id]), "override"
    if name in overrides:
        return int(overrides[name]), "override"
    return int(repeat_default), "default"


def build_chapter_plan(
    tree: Any,
    chapters: Iterable[str],
    limit: int,
    completed_keys: Iterable[str],
    repeat_default: int = 3,
    repeat_overrides: Optional[Dict[str, int]] = None,
    force: bool = False,
) -> List[Task]:
    """v2 计划：选顶层大章 → 每章展开 k=1..M → 生成 大章id::k 执行单元。

    limit 语义为"本次纳入的大章数"。非 force 时把存在未完成次数的大章排在前面，
    使限量名额优先用于待复习大章（沿用 v1 pending 优先思路）；force 保持原顺序。
    """
    chapter_nodes = parsers.top_chapter_nodes(tree)
    overrides = dict(repeat_overrides or {})

    selected = [node for node in chapter_nodes if match_chapter_node(node, chapters)]
    if not selected:
        raise parsers.ParseError("平台页面结构可能变化：所选章节下没有匹配的顶层大章。")

    completed = set(completed_keys or [])
    if not force and completed:
        def has_pending(node: Dict[str, Any]) -> bool:
            node_id = str(node.get("id", "") or "").strip()
            count, _ = _chapter_repeat(node, overrides, repeat_default)
            return any(make_task_key(node_id, k) not in completed
                       for k in range(1, count + 1))

        selected = ([node for node in selected if has_pending(node)] +
                    [node for node in selected if not has_pending(node)])

    total = int(limit or 0)
    if total > 0:
        selected = selected[:total]

    plan: List[Task] = []
    for node in selected:
        count, source = _chapter_repeat(node, overrides, repeat_default)
        name = str(node.get("name", "") or "")
        representative = parsers.representative_file(node, tree)
        for k in range(1, count + 1):
            plan.append(Task.from_chapter_and_repeat(
                node, representative, k, source, chapter_name=name))
    if not plan:
        raise parsers.ParseError("平台页面结构可能变化：未生成任何复习执行单元。")
    return plan


def _build_tasks(tree: Any) -> List[Task]:
    """递归收集所有节点文件，生成去重后的任务全集。"""
    tasks: List[Task] = []
    seen: Set[str] = set()
    for node in parsers.iter_tree_nodes(tree):
        for path in parsers.node_files(node):
            task = Task.from_node_and_path(node, path)
            if not task.unique_key or task.unique_key in seen:
                continue
            seen.add(task.unique_key)
            tasks.append(task)
    return tasks


def build_plan(tree: Any, chapters: Iterable[str], limit: int,
               completed_keys: Iterable[str], force: bool = False) -> List[Task]:
    """构造本次复习计划。

    约定：默认把未完成/失败任务排在已完成之前，使限量名额优先用于待复习任务；
    force=True 时忽略历史完成记录，按目录原顺序执行。计划本身始终包含已完成项，
    便于面板展示完整任务清单；是否跳过由执行器依据 SUCCESS 记录判定。
    """
    tasks = _build_tasks(tree)
    if not tasks:
        raise parsers.ParseError("平台页面结构可能变化：未解析到任何可自测文件（0 个任务）。")

    filtered = [task for task in tasks
                if match_chapter(task.md_path, {"name": task.node_name}, chapters)]
    if not filtered:
        raise parsers.ParseError("平台页面结构可能变化：所选章节下没有可自测文件。")

    completed = set(completed_keys or [])
    if not force and completed:
        pending = [task for task in filtered if task.unique_key not in completed]
        done = [task for task in filtered if task.unique_key in completed]
        filtered = pending + done

    total = int(limit or 0)
    return filtered[:total] if total > 0 else list(filtered)


class Planner:
    """规划器静态门面，签名与类图一致。"""

    @staticmethod
    def build_plan(tree: Any, chapters: Iterable[str], limit: int,
                   completed_keys: Iterable[str], force: bool = False) -> List[Task]:
        return build_plan(tree, chapters, limit, completed_keys, force)

    @staticmethod
    def match_chapter(md_path: str, node: Dict[str, Any], whitelist: Iterable[str]) -> bool:
        return match_chapter(md_path, node, whitelist)

    @staticmethod
    def build_chapter_plan(
        tree: Any, chapters: Iterable[str], limit: int, completed_keys: Iterable[str],
        repeat_default: int = 3, repeat_overrides: Optional[Dict[str, int]] = None,
        force: bool = False,
    ) -> List[Task]:
        return build_chapter_plan(tree, chapters, limit, completed_keys,
                                  repeat_default, repeat_overrides, force)

    @staticmethod
    def match_chapter_node(chapter_node: Dict[str, Any], whitelist: Iterable[str]) -> bool:
        return match_chapter_node(chapter_node, whitelist)

    @staticmethod
    def normalize_md_path(path: str) -> str:
        return normalize_md_path(path)


def parse_limit_argument(value: str, confirmed: bool = False) -> int:
    """CLI 限量参数（语义=本次大章数）；0/ALL 表示全量，且必须二次确认。"""
    text = str(value or "10").strip()
    if text.upper() in ("ALL", "0", "FULL"):
        if not confirmed:
            raise ValueError("全量执行需二次确认：请输入 yes 或 ALL 确认后重试。")
        return 0
    try:
        number = int(text)
    except ValueError as exc:
        raise ValueError("限量必须是正整数，或 ALL 表示全量。") from exc
    if number < 0:
        raise ValueError("限量不能为负数。")
    return number
