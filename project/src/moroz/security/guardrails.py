from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


GuardAction = Literal["allow", "review", "block", "stop", "escalate"]


@dataclass(frozen=True, slots=True)
class GuardDecision:
    action: GuardAction
    code: str


_STOP_RULES = (
    re.compile(
        r"^\s*(?:стоп|stop|отписаться|не\s+пишите\s+мне)"
        r"(?:\s*(?:[.!]|пожалуйста))*\s*$",
        re.IGNORECASE,
    ),
)
_PROMPT_ATTACK_RULES = (
    re.compile(
        r"\b(?:игнорируй|игнорировать|ignore|disregard|override)\b"
        r".{0,80}\b(?:инструкц\w*|instructions?|rules?|правил\w*)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:покажи|раскрой|выведи|повтори|show|reveal|print|repeat)\b"
        r".{0,80}\b(?:системн\w*\s+промпт\w*|system\s+prompt|"
        r"developer\s+instructions?|скрыт\w*\s+инструкц\w*|"
        r"hidden\s+instructions?|canary\s+token)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:покажи|раскрой|выведи|повтори)\b.{0,80}\b"
        r"(?:(?:свои|твои)\s+)?(?:системн\w*|внутренн\w*|скрыт\w*)"
        r"\s+инструкц\w*\b",
        re.IGNORECASE | re.DOTALL,
    ),
)
_MEDICAL_RISK_RULES = (
    re.compile(
        r"\b(?:сильн\w*\s+боль|резк\w*\s+ухудш\w*|обморок\w*|"
        r"потер\w*\s+сознани\w*|не\s+могу\s+дышать|кровотеч\w*|"
        r"severe\s+pain|fainted|cannot\s+breathe|heavy\s+bleeding)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:постав\w*\s+диагноз|назнач\w*\s+лечени\w*|"
        r"diagnose\s+me|prescribe\s+treatment)\b",
        re.IGNORECASE,
    ),
)
_REVIEW_RULES = (
    re.compile(
        r"\b(?:следующ\w*|вложенн\w*|эт\w*)"
        r"(?:\s+\w+){0,4}\s+инструкц\w*"
        r".{0,60}\b(?:правил\w*|выполн\w*|следу\w*)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\bинструкц\w*\b.{0,60}\b(?:вложенн\w*|"
        r"как\s+правил\w*|выполн\w*)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:treat|use|follow)\b.{0,60}\b(?:embedded|attached|following)\b"
        r".{0,40}\binstructions?\b",
        re.IGNORECASE | re.DOTALL,
    ),
)


def _matches(rules: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(rule.search(text) is not None for rule in rules)


def check_input(
    text: str,
    *,
    recent_message_count: int,
    max_length: int = 4000,
    rate_limit: int = 10,
) -> GuardDecision:
    if not text.strip():
        return GuardDecision("block", "empty_input")
    if len(text) > max_length:
        return GuardDecision("block", "input_too_long")
    if recent_message_count > rate_limit:
        return GuardDecision("block", "rate_limit")
    if _matches(_STOP_RULES, text):
        return GuardDecision("stop", "user_stop")
    if _matches(_PROMPT_ATTACK_RULES, text):
        return GuardDecision("block", "prompt_injection")
    if _matches(_MEDICAL_RISK_RULES, text):
        return GuardDecision("escalate", "medical_risk")
    if _matches(_REVIEW_RULES, text):
        return GuardDecision("review", "instruction_review")
    return GuardDecision("allow", "input_allowed")
