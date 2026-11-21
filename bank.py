"""复习题库汇聚：双来源采集、题干 MD5 去重、多来源合并与检索。"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import parsers
from models import QType, Question


class QuestionBank:
    """内存题库；所有题目以 md5(题干原文) 为唯一键。"""

    def __init__(self, questions: Optional[Dict[str, Question]] = None):
        self.questions: Dict[str, Question] = dict(questions or {})

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
            correct_answer = str(item.get("correct_answer") or item.get("answer_text") or "")
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
