"""复习题库汇聚：双来源采集、题干 MD5 去重、多来源合并、检索与本机持久化。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional

import parsers
from models import QType, Question

BANK_SCHEMA_VERSION = 1


class QuestionBank:
    """题库；所有题目以 md5(题干原文) 为唯一键，可持久化到 data/bank.json。"""

    def __init__(self, path: str = "", questions: Optional[Dict[str, Question]] = None):
        self.path = str(path or "")
        self.questions: Dict[str, Question] = dict(questions or {})
        if self.path:
            self.load(self.path)

    # ---------- 本机持久化 ----------
    def load(self, path: str = "") -> int:
        """从 JSON 载入题库；文件不存在或损坏时按空库处理，返回新增题数。"""
        target = str(path or self.path)
        if not target:
            return 0
        self.path = target
        if not os.path.isfile(target):
            return 0
        try:
            with open(target, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return 0
        rows = data.get("questions") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return 0
        before = len(self.questions)
        for row in rows:
            question = Question.from_dict(row)
            if question.content:
                self.add(question)
        return len(self.questions) - before

    def save(self, path: str = "") -> bool:
        """原子写入题库 JSON；未设置 path 时返回 False（纯内存模式）。"""
        target = str(path or self.path)
        if not target:
            return False
        parent = os.path.dirname(os.path.abspath(target))
        os.makedirs(parent, exist_ok=True)
        payload = {
            "schema_version": BANK_SCHEMA_VERSION,
            "questions": [question.to_dict() for question in self.all()],
        }
        temporary = target + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, target)
        self.path = target
        return True

    def add(self, question: Question) -> None:
        """同题合并来源与解析，不覆盖已有内容。"""
        if question is None or not question.content:
            return
        existing = self.questions.get(question.dedup_key)
        if existing:
            existing.merge(question)
        else:
            self.questions[question.dedup_key] = question

    def collect_from_result(self, html: str, chapter: str = "") -> int:
        """从结果页 window.examData 采集；返回新增题目数。"""
        before = len(self.questions)
        for item in parsers.extract_exam_data(html):
            content = str(item.get("content") or item.get("question") or item.get("title") or "")
            if not content:
                continue
            options = self._normalize_options(item.get("options"))
            correct_answer = self._text_answer(
                item.get("correct_answer") or item.get("answer_text"))
            if not correct_answer:
                correct_answer = self._flag_answer(item.get("options"))
            qtype = self._normalize_qtype(item.get("qtype") or item.get("qtype_name") or item.get("type"))
            self.add(Question(
                qid=str(item.get("question_id") or item.get("id") or ""),
                qtype=qtype,
                content=content,
                options=options,
                correct_answer=correct_answer,
                analysis=str(item.get("analysis") or item.get("comment") or ""),
                sources=["AI辅助自测"],
                has_image=("<img" in content.lower() or any("<img" in opt.lower() for opt in options)),
                chapter=str(chapter or ""),
            ))
        return len(self.questions) - before

    def collect_from_conclusion(self, conclusion: Any, chapter: str = "") -> int:
        """从任务 conclusion.low_rate_questions 采集；返回新增题目数。"""
        before = len(self.questions)
        for question in parsers.parse_conclusion(conclusion, chapter):
            if chapter and not question.chapter:
                question.chapter = chapter
            self.add(question)
        return len(self.questions) - before

    @staticmethod
    def _normalize_options(raw: Any) -> List[str]:
        if isinstance(raw, str):
            try:
                import json
                raw = json.loads(raw)
            except (ValueError, TypeError):
                return []
        if not isinstance(raw, list):
            return []
        options: List[str] = []
        for item in raw:
            if isinstance(item, dict):
                options.append(str(item.get("option") or item.get("text") or ""))
            else:
                options.append(str(item or ""))
        return options

    @staticmethod
    def _text_answer(raw: Any) -> str:
        """正确答案可能是文本，也可能是选项文本数组（多选题）；统一为可读文本。"""
        if isinstance(raw, (list, tuple)):
            return "；".join(str(item or "").strip() for item in raw if str(item or "").strip())
        return str(raw or "").strip()

    @staticmethod
    def _flag_answer(raw: Any) -> str:
        """选项对象带 flag=1 时作为正确答案文本。"""
        if not isinstance(raw, list):
            return ""
        texts: List[str] = []
        for item in raw:
            if isinstance(item, dict):
                try:
                    flag = int(item.get("flag", 0) or 0)
                except (TypeError, ValueError):
                    flag = 0
                if flag == 1:
                    texts.append(str(item.get("option") or item.get("text") or ""))
        return "；".join(texts)

    @staticmethod
    def _normalize_qtype(value: Any) -> str:
        text = str(value or "")
        if text == QType.MULTIPLE.value or "多选" in text:
            return QType.MULTIPLE.value
        return QType.SINGLE.value

    def search(self, keyword: str = "", qtype: str = "", chapter: str = "") -> List[Question]:
        """关键词 + 题型 + 章节筛选，供面板与导出复用。"""
        needle = str(keyword or "").strip().lower()
        wanted_type = str(qtype or "").strip()
        wanted_chapter = str(chapter or "").strip()
        results: List[Question] = []
        for question in self.questions.values():
            if wanted_type and question.qtype != wanted_type:
                continue
            if wanted_chapter and wanted_chapter not in (question.chapter or ""):
                continue
            if needle:
                haystack = " ".join([question.content, question.analysis, question.correct_answer] + list(question.options)).lower()
                if needle not in haystack:
                    continue
            results.append(question)
        return results

    def all(self) -> List[Question]:
        """按题干 md5 排序，输出稳定。"""
        return [self.questions[key] for key in sorted(self.questions)]

    def to_dicts(self) -> List[Dict[str, Any]]:
        return [question.to_dict() for question in self.all()]

    def __len__(self) -> int:
        return len(self.questions)
