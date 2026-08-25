from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal


GuardAction = Literal["allow", "review", "block", "stop", "escalate"]


@dataclass(frozen=True, slots=True)
class GuardDecision:
    action: GuardAction
    code: str


_STOP_RULES = (
    re.compile(
        r"^\s*(?:(?:пожалуйста|please)\s*[,!.-]?\s*)?"
        r"(?:стоп|stop|отписаться|отпишите\s+меня|unsubscribe|"
        r"не\s+пишите\s+мне)"
        r"(?:\s*[,!.-]?\s*(?:пожалуйста|please))?\s*[.!]*\s*$",
        re.IGNORECASE,
    ),
)
_PROMPT_ATTACK_RULES = (
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
    re.compile(
        r"\b(?:смени|замени|забудь|переопредели|игнорируй|"
        r"override|replace|forget|ignore|disregard)\b.{0,80}\b"
        r"(?:system\s+(?:prompt|rules?)|"
        r"developer\s+(?:role|instructions?|rules?)|internal\s+rules?|"
        r"системн\w*\s+(?:роль|промпт\w*|инструкц\w*|правил\w*)|"
        r"внутренн\w*\s+(?:инструкц\w*|правил\w*)|"
        r"правил\w*\s+разработчик\w*)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:работай|действуй|work|operate)\b.{0,20}\b"
        r"(?:без\s+ограничен\w*|without\s+restrictions?|"
        r"without\s+limitations?)\b",
        re.IGNORECASE | re.DOTALL,
    ),
)
_PRIVILEGED_CONTEXT_RULES = (
    re.compile(
        r"(?:\b(?:authority|hq)\s+override\b|<\s*system\b|"
        r"\bdebug\b|\bsystem[_ -](?:debug[_ -]log|update|block)\b|"
        r"\badditional\s+instructions?\b|\bsafety[-_ ]module\b|"
        r"\badmin_override\b|\brequired_dump\b|"
        r"\berror\s*\d+\b|\bкалибровк\w*\b|"
        r"\bмодул\w*\s+безопасност\w*\s+упал\w*\b|"
        r"\bвымышленн\w*\s+симуляц\w*\b|"
        r"\bс\s+этого\s+момента\b|\bобнови\s+свою\s+память\b|"
        r"\bдиагностическ\w*\s+модул\w*\b|"
        r"\bбезобидн\w*\s+проверк\w*\b)",
        re.IGNORECASE,
    ),
)
_PROTECTED_TARGET_RULES = (
    re.compile(
        r"(?:\b(?:system[_ ]prompt|instructions?|internal[_ ]variables|"
        r"unrestricted|confidential_access|required_dump)\b|"
        r"\b(?:промпт|инструкц|правил|ограничен|"
        r"внутренн\w*\s+переменн)\w*\b)",
        re.IGNORECASE,
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
    re.compile(
        r"\b(?:гарантир\w*|обеща\w*)\b.{0,40}\b"
        r"(?:вылеч\w*|лечени\w*|медицинск\w*\s+результат\w*)\b",
        re.IGNORECASE | re.DOTALL,
    ),
)
_REVIEW_RULES = (
    re.compile(
        r"\b(?:игнорируй|игнорировать|забудь|ignore|forget|disregard)\b"
        r".{0,30}\b(?:предыдущ\w*|прежн\w*|все|previous|prior|all)\b"
        r".{0,20}\b(?:инструкц\w*|instructions?)\b",
        re.IGNORECASE | re.DOTALL,
    ),
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
_THIRD_PARTY_PII_RULES = (
    re.compile(
        r"\b(?:телефон\w*|номер\w*|адрес\w*|имя|фио|"
        r"медицинск\w*\s+истори\w*|истори\w*\s+посещен\w*|"
        r"запис\w*)\b.{0,60}\b(?:друг\w*\s+клиент\w*|чуж\w*)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:друг\w*\s+клиент\w*|чуж\w*)\b.{0,60}\b"
        r"(?:телефон\w*|номер\w*|адрес\w*|имя|фио|данн\w*|"
        r"медицинск\w*\s+истори\w*|истори\w*\s+посещен\w*|"
        r"запис\w*)\b",
        re.IGNORECASE | re.DOTALL,
    ),
)
_DANGEROUS_CONTENT_RULES = (
    re.compile(
        r"\b(?:напиши|создай|сгенерируй|сделай)\w*\b.{0,50}\b"
        r"(?:вирус\w*|вредонос\w*|фишинг\w*|эксплойт\w*)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:как|инструкц\w*)\b.{0,50}\b"
        r"(?:взлом\w*|отрав\w*|уби(?:ть|йств)\w*|"
        r"изготов\w*\s+(?:оружи\w*|бомб\w*))\b",
        re.IGNORECASE | re.DOTALL,
    ),
)
_OBFUSCATED_INSTRUCTION_RULES = (
    re.compile(
        r"\bbase64\b.{0,60}\b(?:выполн\w*|инструкц\w*|execute|instruction)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b1gn0r3\b.{0,60}\b(?:rul3s|pr0mpt)\b",
        re.IGNORECASE | re.DOTALL,
    ),
)
_REPORT_OR_HANDOFF_RULES = (
    re.compile(
        r"\b(?:жалоб\w*|пожаловат\w*|мне\s+(?:прислал\w*|показал\w*)|"
        r"соедините|позовите|попытк\w*)\b",
        re.IGNORECASE,
    ),
)
_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\ufeff\u2060]")


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
    check_text = _ZERO_WIDTH_RE.sub(
        "",
        unicodedata.normalize("NFKC", text),
    ).casefold()
    if _matches(_STOP_RULES, check_text):
        return GuardDecision("stop", "user_stop")
    if _matches(_PROMPT_ATTACK_RULES, check_text):
        return GuardDecision("block", "prompt_injection")
    if _matches(_PRIVILEGED_CONTEXT_RULES, check_text) and _matches(
        _PROTECTED_TARGET_RULES,
        check_text,
    ):
        return GuardDecision("block", "prompt_injection")
    reports_problem = _matches(_REPORT_OR_HANDOFF_RULES, check_text)
    if reports_problem and (
        _matches(_THIRD_PARTY_PII_RULES, check_text)
        or _matches(_DANGEROUS_CONTENT_RULES, check_text)
    ):
        return GuardDecision("review", "reported_security_issue")
    if _matches(_THIRD_PARTY_PII_RULES, check_text):
        return GuardDecision("block", "third_party_pii")
    if _matches(_DANGEROUS_CONTENT_RULES, check_text):
        return GuardDecision("block", "dangerous_content")
    if _matches(_MEDICAL_RISK_RULES, check_text):
        return GuardDecision("escalate", "medical_risk")
    if _matches(_OBFUSCATED_INSTRUCTION_RULES, check_text):
        return GuardDecision("review", "obfuscated_instruction")
    if _matches(_REVIEW_RULES, check_text):
        return GuardDecision("review", "instruction_review")
    return GuardDecision("allow", "input_allowed")
