"""纯解析函数：HTML/JSON → 领域对象，绝不触网，可离线单测。"""
from __future__ import annotations

import ast
import html as html_module
import json
import re
from typing import Any, Dict, Iterator, List, Optional

from models import QType, Question


class ParseError(Exception):
    """页面结构与预期不符；统一提示"平台页面结构可能变化"。"""


def _balanced_slice(text: str, start: int, opening: str, closing: str) -> str:
    """从 start 处的开括号开始，返回到配对闭括号的切片（忽略字符串内括号）。"""
    depth = 0
    in_string = False
    quote = ""
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in ("'", '"'):
            in_string = True
            quote = char
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise ParseError("平台页面结构可能变化：无法定位完整的数据对象。")


def _loads_js_object(raw: str) -> Any:
    """优先 json.loads，兼容单引号/undefined 等轻量 JS 字面量。"""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass
    candidate = raw.replace("undefined", "null")
    candidate = re.sub(r"(\w+)\s*:", r'"\1":', candidate)
    candidate = candidate.replace("'", '"')
    try:
        return json.loads(candidate)
    except (ValueError, TypeError):
        try:
            return ast.literal_eval(raw)
        except (ValueError, SyntaxError) as exc:
            raise ParseError("平台页面结构可能变化：数据对象无法解析。") from exc


def parse_graph_data(html: str) -> Dict[str, Any]:
    """从 var graphData={...} 提取课程树；失败抛 ParseError。"""
    text = str(html or "")
    marker = re.search(r"(?:var|let|const)?\s*(?:window\.)?graphData\s*=\s*", text)
    if not marker:
        raise ParseError("平台页面结构可能变化：未在资料页找到 graphData。")
    brace = text.find("{", marker.end())
    if brace < 0:
        raise ParseError("平台页面结构可能变化：graphData 不是对象结构。")
    try:
        data = _loads_js_object(_balanced_slice(text, brace, "{", "}"))
    except ParseError:
        raise
    except Exception as exc:  # 兜底为可读 ParseError，不向上传播原始堆栈
        raise ParseError(f"平台页面结构可能变化：graphData 解析失败（{type(exc).__name__}）。") from exc
    if not isinstance(data, dict) or "tree" not in data:
        raise ParseError("平台页面结构可能变化：graphData 缺少 tree 字段。")
    return data


def iter_tree_nodes(tree: Any) -> Iterator[Dict[str, Any]]:
    """递归遍历整棵树，兼容 dict/list，自动发现任意子节点数组字段。"""
    seen: set = set()
    stack: List[Any] = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            for child in reversed(node):
                stack.append(child)
            continue
        if not isinstance(node, dict):
            continue
        identity = id(node)
        if identity in seen:
            continue
        seen.add(identity)
        if node.get("id") not in (None, ""):
            yield node
        children = [value for value in node.values() if isinstance(value, (dict, list))]
        for child in reversed(children):
            stack.append(child)


def node_files(node: Dict[str, Any]) -> List[str]:
    """提取节点自身的 md 文件相对路径数组。"""
    raw = (node or {}).get("files")
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item or "").lower().endswith(".md")]
    if isinstance(raw, str) and raw.strip().lower().endswith(".md"):
        return [raw]
    return []


# 顶层大章子节点数组字段优先级；不写死任何具体大章 id。
_TOP_CHILD_KEYS = ("children", "nodes", "items")


def top_chapter_nodes(tree: Any) -> List[Dict[str, Any]]:
    """取 graphData.tree 的顶层大章节点。

    tree 为 list 时直接取其元素；为 dict 时优先取 children/nodes/items 中第一个数组，
    再退回其它数组字段（排除 files）。仅保留含 id 的 dict 节点；为空抛 ParseError。
    """
    raw: List[Any] = []
    if isinstance(tree, list):
        raw = list(tree)
    elif isinstance(tree, dict):
        for key in _TOP_CHILD_KEYS:
            value = tree.get(key)
            if isinstance(value, list):
                raw = value
                break
        if not raw:
            for key, value in tree.items():
                if str(key).lower() != "files" and isinstance(value, list):
                    raw = value
                    break
    chapters: List[Dict[str, Any]] = [
        node for node in raw
        if isinstance(node, dict) and str(node.get("id", "") or "").strip()
    ]
    if not chapters:
        raise ParseError("平台页面结构可能变化：未识别到顶层大章节点")
    return chapters


def representative_file(chapter: Dict[str, Any], tree: Any = None) -> str:
    """大章代表文件：优先 chapter['files'][0]，否则子树内第一个 .md，找不到返回 ''。

    tree 参数为接口对称性保留；代表文件仅作为组卷请求参数，不影响题目归属。
    """
    files = node_files(chapter)
    if files:
        return files[0]
    for node in iter_tree_nodes(chapter):
        found = node_files(node)
        if found:
            return found[0]
    return ""


def _parse_value(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return None
    if value[0] in "{[":
        opening, closing = ("{", "}") if value[0] == "{" else ("[", "]")
        return _loads_js_object(_balanced_slice(value, 0, opening, closing))
    if value[0] in "\"'":
        quote = value[0]
        end = 1
        while end < len(value):
            if value[end] == "\\":
                end += 2
                continue
            if value[end] == quote:
                literal = value[:end + 1]
                if quote == '"':
                    try:
                        return json.loads(literal)
                    except ValueError:
                        pass
                return literal[1:-1].replace('\\"', '"').replace("\\/", "/")
            end += 1
        return value[1:]
    value = re.split(r"[;\n]", value, maxsplit=1)[0].strip().rstrip(",")
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def extract_window_var(html: str, name: str) -> Any:
    """提取 window.<name>（或同名全局）的值；不存在返回 None。"""
    text = str(html or "")
    key = re.escape(str(name))
    patterns = (
        rf"(?:window\.)?{key}\s*=\s*",
        rf"[\"']{key}[\"']\s*:\s*",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        cursor = match.end()
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text):
            return None
        try:
            return _parse_value(text[cursor:])
        except ParseError:
            return None
    return None


def extract_exam_data(html: str) -> List[Dict[str, Any]]:
    """解析结果页 window.examData，统一返回题目对象数组。

    2026-11-23 实测：平台真实结构是「题型 → 分桶对象」：

        window.examData = {"qt01": {"count":5, "qtype_name":"单选题",
            "questions":[{"content":..., "correct_answer":...,
                          "analysis":..., "options":[...]}]}}

    旧实现只认顶层数组/单层 questions，会把整个分桶当成一个"题目"，
    再因没有 content 被题库丢弃——结果页有答案和解析，题库却一题都没有。
    这里兼容三种形态：题型分桶、单层数组、直接题目对象。
    """
    raw = extract_window_var(html, "examData")
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if isinstance(raw, dict):
        # 真实形态：题型 → {..., questions: [题目]}。按题型展开并回填题型字段。
        bucketed: List[Dict[str, Any]] = []
        for key, bucket in raw.items():
            if not isinstance(bucket, dict):
                continue
            items = bucket.get("questions")
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                item.setdefault("qtype", str(key))
                if bucket.get("qtype_name"):
                    item.setdefault("qtype_name", str(bucket["qtype_name"]))
                bucketed.append(item)
        if bucketed:
            return bucketed
        # 兼容形态：{questions|data|list|items: [题目]}
        for key in ("questions", "data", "list", "items", "examData"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
        else:
            raw = [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def extract_score(html: str) -> Optional[float]:
    """从结果页提取最终得分/已评阅分数；未评阅返回 None。"""
    text = re.sub(r"<[^>]+>", " ", str(html or ""))
    text = html_module.unescape(text)
    for pattern in (
        r"(?:最终得分|已评阅|最终成绩|得分)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*分?",
        r"(?:score|final_score)\s*[:=]\s*(\d+(?:\.\d+)?)",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return None


def parse_conclusion(conclusion: Any, chapter: str = "") -> List[Question]:
    """解析任务 conclusion 中的 low_rate_questions 为题目列表。"""
    if isinstance(conclusion, str):
        try:
            data = json.loads(conclusion)
        except (ValueError, TypeError):
            return []
    elif isinstance(conclusion, dict):
        data = conclusion
    else:
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("low_rate_questions") or []
    if not isinstance(items, list):
        return []

    questions: List[Question] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        options: List[str] = []
        correct_answer = ""
        raw_options = item.get("options") or []
        if isinstance(raw_options, list):
            for option in raw_options:
                if isinstance(option, dict):
                    text = str(option.get("option", "") or "")
                    options.append(text)
                    try:
                        flag = int(option.get("flag", 0) or 0)
                    except (TypeError, ValueError):
                        flag = 0
                    if flag == 1 and not correct_answer:
                        correct_answer = text
                else:
                    options.append(str(option))
        correct_answer = str(item.get("correct_answer") or item.get("answer_text") or correct_answer)
        qtype_name = str(item.get("qtype_name") or "")
        qtype = QType.MULTIPLE.value if qtype_name == QType.MULTIPLE.value or "多选" in qtype_name else QType.SINGLE.value
        content = str(item.get("content") or "")
        analysis = str(item.get("reason") or item.get("analysis") or "")
        questions.append(Question(
            qid=str(item.get("question_id") or ""),
            qtype=qtype,
            content=content,
            options=options,
            correct_answer=correct_answer,
            analysis=analysis,
            sources=["AI评阅总结"],
            has_image="<img" in content.lower(),
            chapter=chapter,
        ))
    return questions


def parse_question_types(payload: Any) -> Dict[str, str]:
    """题型接口 → {qt01: 单选题, ...}。"""
    result: Dict[str, str] = {}
    if isinstance(payload, dict):
        items = payload.get("data") or payload.get("types") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                result[str(item["id"])] = str(item.get("name") or item["id"])
    return result


def normalize_questions(questions: Any) -> Dict[str, List[Dict[str, Any]]]:
    """组卷返回 → {qtype: [题目, ...]}。

    2026-11-21 实测：平台 `/功能/api/selftest/generate` 返回的 `questions` 里，
    每个题型是一个**对象**（即交卷时的 task_data 分桶结构）：

        {"qt01": {"count": 5, "answer_count": 5, "qtype_name": "单选题",
                  "questions": [{"id": ..., "content": ..., "options": [...]}]}}

    早期实现把它当成数组处理（`if isinstance(items, list)`），导致每桶都被判为
    空列表，最终解析出 0 道题、交了空卷、得分 0 却仍记 success。此处同时兼容
    「题型 → 数组」与「题型 → 对象（内含 questions 数组）」两种形态。
    """
    normalized: Dict[str, List[Dict[str, Any]]] = {}
    if not isinstance(questions, dict):
        return normalized
    for qtype, bucket in questions.items():
        items: Any = bucket
        if isinstance(bucket, dict):
            items = bucket.get("questions")
        if not isinstance(items, list):
            items = []
        normalized[str(qtype)] = [item for item in items if isinstance(item, dict)]
    return normalized


def count_questions(normalized: Dict[str, List[Dict[str, Any]]]) -> int:
    """统计归一化后的题目总数，用于「空卷」防御。"""
    if not isinstance(normalized, dict):
        return 0
    return sum(len(items) for items in normalized.values() if isinstance(items, list))

