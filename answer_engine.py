"""作答引擎：平台 HTTP 与作答决策解耦，chat_fn 可注入替身。"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from models import AnswerDetail, QType
from utils import truncate


class AnswerParseError(Exception):
    """无法把作答返回解析为明确选项；记 ANSWER_PARSE，不猜测。"""

    error_type = "ANSWER_PARSE"


def _field(question: Any, name: str, default: Any = None) -> Any:
    if isinstance(question, dict):
        return question.get(name, default)
    return getattr(question, name, default)


def _options_of(question: Any) -> List[str]:
    raw = _field(question, "options", []) or []
    if isinstance(raw, str):
        try:
            import json
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = []
    if not isinstance(raw, list):
        return []
    return [str(item or "") for item in raw]


def _is_multiple(question: Any) -> bool:
    qtype = str(_field(question, "qtype", "") or "")
    return qtype in (QType.MULTIPLE.value, "multiple", "多选") or "多选" in qtype


def selection_to_texts(question: Any, selection: str) -> List[str]:
    """把 A,B 形式的选择映射为选项文本数组。"""
    options = _options_of(question)
    texts: List[str] = []
    for letter in re.split(r"[,，、\s]+", str(selection or "").strip()):
        letter = letter.strip().upper()
        if len(letter) != 1 or not ("A" <= letter <= "Z"):
            continue
        index = ord(letter) - ord("A")
        if 0 <= index < len(options):
            texts.append(options[index])
    return texts


class AnswerEngine(ABC):
    """作答接口；chat_fn 签名为 (message: str) -> str。"""

    @abstractmethod
    def answer(self, question: Any, chat_fn: Callable[[str], str]) -> AnswerDetail:
        """调用问答能力并返回作答详情。"""

    @abstractmethod
    def parse_option(self, response: str, question: Any) -> str:
        """把原始返回解析为明确选项；单选返回 "B"，多选返回 "A,C"。"""


class PlatformChatAnswerEngine(AnswerEngine):
    """基于平台 /api/chat 的作答实现，保留原始返回用于复盘。"""

    DEFAULT_PROMPT = (
        "你是课程自测助手。请依据题目内容选择正确选项，只返回选项字母，不要解释："
        "单选题返回一个字母（如 B）；多选题返回多个字母并用英文逗号分隔（如 A,C）。\n"
        "题目：{content}\n选项：\n{options}\n你的答案："
    )

    def __init__(self, model: str = "deepseek-v4-flash", prompt_template: Optional[str] = None):
        self.model = str(model or "deepseek-v4-flash")
        self.prompt_template = str(prompt_template or self.DEFAULT_PROMPT)

    def build_prompt(self, question: Any) -> str:
        options = _options_of(question)
        lines = [f"{chr(65 + index)}. {text}" for index, text in enumerate(options)]
        return self.prompt_template.format(
            content=str(_field(question, "content", "") or ""),
            options="\n".join(lines),
        )

    def parse_option(self, response: str, question: Any) -> str:
        text = str(response or "").strip()
        if not text:
            raise AnswerParseError("作答返回为空，无法解析为选项。")
        options = _options_of(question)
        count = len(options)
        if count <= 0:
            raise AnswerParseError("题目缺少选项，无法作答。")
        first_line = re.split(r"[\n。；;]", text, maxsplit=1)[0].strip()
        cleaned = re.sub(r"^(?:答案|正确答案|选项|选择|answer)\s*[:：是为]?\s*", "", first_line,
                         flags=re.IGNORECASE).strip()

        # 数字序号答案：1 / 1,3（仅当没有字母时尝试，避免误读题干里的数字）
        if not re.search(r"[A-Za-z]", cleaned):
            numbers = re.findall(r"(?<!\d)(\d+)(?!\d)", cleaned)
            if numbers:
                letters = []
                for number in numbers:
                    index = int(number) - 1
                    if 0 <= index < count:
                        letters.append(chr(65 + index))
                if letters:
                    return ",".join(dict.fromkeys(letters))

        upper = cleaned.upper()
        found = re.findall(r"(?<![A-Za-z])([A-Z])(?![A-Za-z])", upper)
        letters = [letter for letter in dict.fromkeys(found) if ord(letter) - ord("A") < count]
        if letters:
            return ",".join(letters)

        # 直接返回选项文本也算明确选择
        normalized = cleaned.strip().strip("。,.，")
        for index, option in enumerate(options):
            if normalized and (normalized == option.strip() or normalized in option.strip()):
                return chr(65 + index)
        raise AnswerParseError("返回内容中没有明确的选项标识。")

    def answer(self, question: Any, chat_fn: Callable[[str], str]) -> AnswerDetail:
        qid = str(_field(question, "id", "") or "")
        session_id = str(_field(question, "session_id", "") or "")
        prompt = self.build_prompt(question)
        try:
            raw = chat_fn(prompt)
        except Exception as exc:  # 问答服务异常不中断整体计划，交给执行器记失败
            return AnswerDetail(
                session_id=session_id, question_id=qid, raw_response=truncate(str(exc), 500),
                final_selection="", selected_text="", ok=False,
            )
        raw_text = "" if raw is None else str(raw)
        try:
            selection = self.parse_option(raw_text, question)
            ok = bool(selection)
        except AnswerParseError as exc:
            selection = ""
            ok = False
            raw_text = (raw_text + f"\n[解析失败] {exc}").strip()
        texts = selection_to_texts(question, selection)
        return AnswerDetail(
            session_id=session_id,
            question_id=qid,
            raw_response=raw_text,
            final_selection=selection,
            selected_text="；".join(texts),
            comment_score=0.0,
            ok=ok,
        )
