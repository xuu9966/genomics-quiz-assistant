"""题库与记录导出：HTML / Markdown / JSON / CSV，统一脱敏、无凭据。"""
from __future__ import annotations

import contextlib
import csv
import html
import io
import json
import re
from typing import Any, Iterable, List, TextIO

from models import ExecutionRecord, Question
from utils import mask


@contextlib.contextmanager
def _text_target(target: Any) -> Iterable[TextIO]:
    """target 可为路径字符串或已打开的文本流（供 Web API 复用）。"""
    if hasattr(target, "write"):
        yield target
        return
    with open(str(target), "w", encoding="utf-8", newline="") as handle:
        yield handle


def _record_dict(record: Any) -> dict:
    if isinstance(record, dict):
        return dict(record)
    if hasattr(record, "to_dict"):
        return record.to_dict()
    return {}


def _question_dict(question: Any) -> dict:
    if isinstance(question, dict):
        return dict(question)
    if hasattr(question, "to_dict"):
        return question.to_dict()
    return {}


def _safe_masked(value: Any) -> str:
    return mask("" if value is None else str(value))


def _correct_labels(question: Question) -> List[str]:
    """返回正确选项字母列表，支持字母答案与文本答案。"""
    answer = str(question.correct_answer or "").strip()
    labels: List[str] = []
    if answer.isalpha() and 0 < len(answer) <= max(1, len(question.options)):
        for char in answer.upper():
            if "A" <= char <= "Z" and char not in labels:
                labels.append(char)
        if labels:
            return labels
    for index, option in enumerate(question.options):
        option_text = str(option or "").strip()
        if answer and option_text and (answer == option_text or answer in option_text):
            label = chr(65 + index)
            if label not in labels:
                labels.append(label)
    return labels


def _safe_image_src(url: str) -> str:
    """只允许 http(s)/data 图片地址，避免导出物注入本地文件路径。"""
    value = str(url or "").strip()
    lowered = value.lower()
    if lowered.startswith(("http://", "https://", "data:image/")):
        return html.escape(value, quote=True)
    return ""


def _image_sources(text: str) -> List[str]:
    """从题面原文提取图片地址；仅保留安全的 http(s)/data 地址。"""
    sources: List[str] = []
    for match in re.finditer(r"<img[^>]*?src\s*=\s*[\"']([^\"']+)[\"'][^>]*>", str(text or ""), re.IGNORECASE):
        safe = _safe_image_src(match.group(1))
        if safe and safe not in sources:
            sources.append(safe)
    return sources


def bank_html(questions: Iterable[Any], path: Any) -> None:
    """导出可筛选、可展开、可打印的复习题库 HTML。"""
    items = [_question_dict(question) for question in questions]
    cards: List[str] = []
    for index, raw in enumerate(items, start=1):
        q = Question(
            dedup_key=str(raw.get("dedup_key") or ""),
            qid=str(raw.get("qid") or ""),
            qtype=str(raw.get("qtype") or "qt01"),
            content=str(raw.get("content") or ""),
            options=[str(item) for item in (raw.get("options") or [])],
            correct_answer=str(raw.get("correct_answer") or ""),
            analysis=str(raw.get("analysis") or ""),
            sources=[str(item) for item in (raw.get("sources") or [])],
            has_image=bool(raw.get("has_image")),
            chapter=str(raw.get("chapter") or ""),
        )
        qtype_text = "多选题" if q.qtype == "qt02" else "单选题"
        labels = set(_correct_labels(q))
        option_lines = []
        for position, option in enumerate(q.options):
            label = chr(65 + position)
            image = _safe_image_src(option) if "<img" in option.lower() else ""
            option_html = html.escape(_safe_masked(option))
            if image:
                option_html += f' <img src="{image}" alt="题面图片">'
            mark = " ✓ 正确" if label in labels else ""
            option_lines.append(f'<li><span class="label">{label}.</span> {option_html}<span class="mark">{mark}</span></li>')
        content = html.escape(_safe_masked(q.content))
        for source in _image_sources(q.content):
            content += f' <img src="{source}" alt="题面图片">'
        if "<img" in q.content.lower():
            content += " <span class=\"note\">[含图片，请查看原始题面]</span>"
        analysis = html.escape(_safe_masked(q.analysis)).replace("\n", "<br>")
        sources = html.escape(_safe_masked(" / ".join(q.sources)))
        keyword = html.escape(_safe_masked(" ".join([q.content, q.analysis] + q.options)).lower(), quote=True)
        cards.append(
            f'<details class="question" data-keyword="{keyword}">'
            f'<summary>Q{index}（{qtype_text}）[{html.escape(_safe_masked(q.chapter or "未分类"))}] {content}</summary>'
            f'<ol class="options">{"".join(option_lines)}</ol>'
            f'<p>正确答案：{html.escape(_safe_masked(q.correct_answer))}</p>'
            f'<p>解析：{analysis or "—"}</p>'
            f'<p class="note">来源：{sources or "—"}　提示：建议先自测，再展开解析。</p>'
            f'</details>'
        )

    document = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>复习题库</title>
<style>
body{font-family:"Noto Serif SC","Songti SC",Georgia,serif;color:#222;background:#fff;margin:0 auto;padding:24px;max-width:960px;line-height:1.7;}
h1{font-size:22px;font-weight:600;border-bottom:1px solid #333;padding-bottom:8px;}
.meta{color:#666;font-size:13px;margin-bottom:16px;}
.toolbar{border:1px solid #ccc;padding:10px;margin-bottom:16px;}
input[type=search]{border:1px solid #999;padding:4px 8px;width:260px;background:#fff;color:#222;}
.question{border:1px solid #bbb;margin:8px 0;padding:8px 12px;user-select:text;}
.question summary{cursor:pointer;font-weight:600;}
.options{margin:8px 0 8px 18px;}
.label{font-weight:600;}
.mark{font-weight:600;}
.note{color:#666;font-size:12px;}
img{max-width:100%;height:auto;border:1px solid #ddd;}
@media print{.toolbar{display:none}.question{break-inside:avoid}body{padding:0}}
</style></head><body>
<h1>复习题库</h1>
<p class="meta">共 __N__ 题（已按题干去重）　导出前已做凭据脱敏；仅供个人复习，不分发不商用。</p>
<div class="toolbar"><label>关键词筛选：<input type="search" id="filter" placeholder="输入题目/解析关键词"></label></div>
<div id="list">__CARDS__</div>
<p class="note" id="empty" hidden>没有匹配的题目。</p>
<script>
var box=document.getElementById('filter'), cards=[].slice.call(document.querySelectorAll('.question')), empty=document.getElementById('empty');
box.addEventListener('input',function(){var v=box.value.trim().toLowerCase(), shown=0;cards.forEach(function(c){var ok=!v||c.getAttribute('data-keyword').indexOf(v)>=0;c.hidden=!ok;if(ok)shown++;});empty.hidden=shown>0;});
</script>
</body></html>""".replace("__N__", str(len(items))).replace("__CARDS__", "\n".join(cards))
    with _text_target(path) as handle:
        handle.write(document)


def bank_markdown(questions: Iterable[Any], path: Any) -> None:
    """导出适合打印/复制的 Markdown；图片题明确标记。"""
    items = list(questions)
    lines = ["# 复习题库", "", f"> 共 {len(items)} 题（已按题干去重）", ""]
    for index, raw in enumerate(items, start=1):
        q = Question(
            qid=str(raw.get("qid") or "") if isinstance(raw, dict) else str(getattr(raw, "qid", "")),
            qtype=str(raw.get("qtype") or "qt01") if isinstance(raw, dict) else str(getattr(raw, "qtype", "qt01")),
            content=str(raw.get("content") or "") if isinstance(raw, dict) else str(getattr(raw, "content", "")),
            options=list(raw.get("options") or []) if isinstance(raw, dict) else list(getattr(raw, "options", [])),
            correct_answer=str(raw.get("correct_answer") or "") if isinstance(raw, dict) else str(getattr(raw, "correct_answer", "")),
            analysis=str(raw.get("analysis") or "") if isinstance(raw, dict) else str(getattr(raw, "analysis", "")),
            sources=list(raw.get("sources") or []) if isinstance(raw, dict) else list(getattr(raw, "sources", [])),
            has_image=bool(raw.get("has_image")) if isinstance(raw, dict) else bool(getattr(raw, "has_image", False)),
            chapter=str(raw.get("chapter") or "") if isinstance(raw, dict) else str(getattr(raw, "chapter", "")),
        )
        qtype_text = "多选题" if q.qtype == "qt02" else "单选题"
        lines.extend([f"## Q{index}（{qtype_text}）[{q.chapter or '未分类'}]", "", _safe_masked(q.content), ""])
        if q.has_image:
            lines.extend(["> [含图片，请查看原始题面]", ""])
        for position, option in enumerate(q.options):
            label = chr(65 + position)
            lines.append(f"- {label}. {_safe_masked(option)}")
        lines.extend([
            "",
            f"**正确答案**：{_safe_masked(q.correct_answer)}",
            "",
            f"**解析**：{_safe_masked(q.analysis) or '—'}",
            "",
            f"来源：{_safe_masked(' / '.join(q.sources)) or '—'}",
            "",
            "> 提示：建议先自测，再查看解析。",
            "",
        ])
    with _text_target(path) as handle:
        handle.write("\n".join(lines))


def bank_json(questions: Iterable[Any], path: Any) -> None:
    """导出完整字段 JSON；所有文本字段统一脱敏。"""
    data = []
    for raw in questions:
        item = _question_dict(raw)
        data.append({key: _safe_masked(value) if isinstance(value, str) else value
                     for key, value in item.items()})
    with _text_target(path) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def records_csv(records: Iterable[Any], path: Any) -> None:
    """导出执行记录 CSV；密码字段从不出现，所有值脱敏。"""
    # 旧列全部保留，末尾追加 v2 的次数与来源列，缺字段时写空串。
    columns = ["unique_key", "status", "score", "session_id", "error_type",
               "error_summary", "attempts", "started_at", "finished_at",
               "node_id", "md_path", "file_name", "chapter",
               "repeat_index", "repeat_source"]
    with _text_target(path) as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for raw in records:
            data = _record_dict(raw)
            record = ExecutionRecord.from_dict(data) if data else ExecutionRecord()
            row = record.to_dict()
            row.update({
                "node_id": data.get("node_id", ""),
                "md_path": data.get("md_path", ""),
                "file_name": data.get("file_name", ""),
                "chapter": data.get("chapter", ""),
                "repeat_index": data.get("repeat_index", ""),
                "repeat_source": data.get("repeat_source", ""),
            })
            writer.writerow({key: _safe_masked(row.get(key, "")) for key in columns})


def bank_bytes(fmt: str, questions: Iterable[Any]) -> bytes:
    """供 Web API 使用：导出到内存并返回字节。"""
    buffer = io.StringIO()
    fmt = str(fmt or "html").lower()
    if fmt == "json":
        bank_json(questions, buffer)
    elif fmt in ("md", "markdown"):
        bank_markdown(questions, buffer)
    else:
        bank_html(questions, buffer)
    return buffer.getvalue().encode("utf-8")
