"""Guardrails — защита от jailbreak и валидация ответов LLM.

Слой 1: проверка длины + regex-фильтрация входа (check_input)
Слой 2: санитизация входа перед отправкой в LLM (sanitize_input)
Слой 3: валидация ответов LLM на утечку промпта (check_output)

Источники атак: классические jailbreak-техники (DAN, role_switch,
prompt_leak, authority override, system_tags, policy_patch и т.п.).
"""

import logging
import re
import unicodedata

from config import (
    MAX_INPUT_LENGTH,
    GUARDRAILS_INPUT_ENABLED,
    GUARDRAILS_INPUT_CATEGORIES,
    GUARDRAILS_OUTPUT_ENABLED,
)

logger = logging.getLogger(__name__)

# Универсальное сообщение при блокировке (не раскрывает причину)
GUARDRAIL_REFUSAL = (
    "Я не могу ответить на этот запрос. Пожалуйста, переформулируйте вопрос."
)

GUARDRAIL_OUTPUT_FALLBACK = (
    "Извините, произошла ошибка при обработке ответа. Пожалуйста, переформулируйте вопрос."
)

_ZERO_WIDTH_RE = re.compile("[​‌‍﻿⁠]")


# --- Слой 1: regex-фильтрация входа ---

_INPUT_PATTERNS: list[tuple[str, re.Pattern]] = []


def _add(category: str, pattern: str, flags: int = re.IGNORECASE) -> None:
    _INPUT_PATTERNS.append((category, re.compile(pattern, flags)))


# Смена роли / поведения
_add("role_switch", r"забудь.{0,20}(инструкции|правила|указания|промпт)")
_add("role_switch", r"игнорируй.{0,20}(инструкции|правила|указания|промпт)")
_add("role_switch", r"теперь ты\b")
_add("role_switch", r"представь.{0,5}что ты")
_add("role_switch", r"притворись.{0,5}что ты")
_add("role_switch", r"(действуй|веди себя) как")
_add("role_switch", r"ты (больше не|теперь не)")
_add("role_switch", r"(с этого момента|отныне).{0,30}(отвечай|будь|действуй|веди)")
_add("role_switch", r"ignore.{0,20}(instructions|rules|prompt|constraints)")
_add("role_switch", r"forget.{0,20}(instructions|rules|prompt|constraints)")
_add("role_switch", r"(you are|now you.re) now")
_add("role_switch", r"pretend (you are|to be)")
_add("role_switch", r"act as\b")
_add("role_switch", r"from now on")

# Утечка промпта
_add("prompt_leak", r"(покажи|выведи|напиши|повтори|скопируй|расскажи).{0,20}(промпт|промт|prompt|инструкци|правила)")
_add("prompt_leak", r"(какие|какой|какова|что).{0,10}(у тебя|твои|твой).{0,10}(инструкции|промпт|правила|указания)")
_add("prompt_leak", r"(repeat|show|display|print|output|reveal|dump).{0,15}(prompt|instructions|rules|system)")
_add("prompt_leak", r"(свои|твои) (правила|инструкции).{0,20}(новому|другому|кому-то)")

# Авторитетные команды
_add("authority", r"\[(authority|admin|hq|system).{0,20}override")
_add("authority", r"\btier\s*\d+\b")
_add("authority", r"\blevel\s*\d+\b.{0,20}(override|command|order)")
_add("authority", r"(as your|как твой).{0,20}(administrator|supervisor|админ|начальник)")

# Системные сообщения / теги
_add("system_tags", r"<\s*/?system\s*>")
_add("system_tags", r"<\s*/?debug\b")
_add("system_tags", r"<\s*/?admin\b")
_add("system_tags", r"<\s*/?override\b")
_add("system_tags", r"<\s*/?reveal\b")
_add("system_tags", r"<\||\|>")
_add("system_tags", r"\[INST\]|\[/INST\]")
_add("system_tags", r'\{".*admin_override|required_dump')

# Патчи / обновления / деактивация безопасности
_add("policy_patch", r"(system|policy|security).{0,10}(update|patch|upgrade)\s*v?\d")
_add("policy_patch", r"safety.{0,10}(module|mode|system).{0,10}(off|disabled|offline|деактив)")
_add("policy_patch", r"(update|обнови).{0,10}(your memory|свою память|правила)")
_add("policy_patch", r"(new rule|новое правило):")
_add("policy_patch", r"error\s+\d{3}.*missing.{0,20}variable")

# Разделители-инъекции (только на отдельной строке)
_add("separator", r"^-{4,}.*(?:system|block|start|end|override)", re.IGNORECASE | re.MULTILINE)
_add("separator", r"^={4,}", re.IGNORECASE | re.MULTILINE)
_add("separator", r"^----\s*(system|end|start|block)", re.IGNORECASE | re.MULTILINE)

# Известные атаки по имени
_add("known_attack", r"\bDAN\b")
_add("known_attack", r"\bjailbreak\b")
_add("known_attack", r"\bdeveloper mode\b")
_add("known_attack", r"\bsudo mode\b")
_add("known_attack", r"\bunrestricted\b.{0,20}(mode|output|access)")


def check_input(text: str) -> tuple[bool, str | None]:
    """Проверить входящее сообщение. Возвращает (ok, причина_блокировки).

    Длина проверяется ВСЕГДА (защита от расходов LLM).
    Regex-категории проверяются только при GUARDRAILS_INPUT_ENABLED=true
    и непустом GUARDRAILS_INPUT_CATEGORIES.
    """
    if len(text) > MAX_INPUT_LENGTH:
        return False, "length: сообщение слишком длинное"

    if not GUARDRAILS_INPUT_ENABLED or not GUARDRAILS_INPUT_CATEGORIES:
        return True, None

    normalized = unicodedata.normalize("NFC", text)
    normalized = _ZERO_WIDTH_RE.sub("", normalized)
    check_text = normalized.lower()

    for category, pattern in _INPUT_PATTERNS:
        if category not in GUARDRAILS_INPUT_CATEGORIES:
            continue
        if pattern.search(check_text):
            return False, f"{category}: {pattern.pattern}"

    return True, None


# --- Слой 2: санитизация входа ---

_TAG_RE = re.compile(r"<[^>]{1,100}>")
_DASHES_RE = re.compile(r"^-{3,}\s*$", re.MULTILINE)
_EQUALS_RE = re.compile(r"^={3,}\s*$", re.MULTILINE)


def sanitize_input(text: str) -> str:
    """Удалить потенциально опасные структуры перед отправкой в LLM."""
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    text = _DASHES_RE.sub("", text)
    text = _EQUALS_RE.sub("", text)
    text = unicodedata.normalize("NFC", text)
    return text


# --- Слой 3: валидация ответа ---

_OUTPUT_LEAK_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        # Универсальные фразы-индикаторы утечки
        r"системный промпт",
        r"system prompt",
        r"мои инструкции",
        r"мне запрещено раскрывать",
        r"мои правила не позволяют",
        r"my (system )?instructions",
        r"my system prompt",
        # Технические маркеры формата промптов
        r"\[INST\]",
        r"<\|im_start\|>",
        r"<\|im_end\|>",
        r"<\|system\|>",
        # Универсальные маркеры из anti-injection preamble (см. _guardrails_preamble.md)
        r"anti-?injection",
        # Имена внутренних модулей (нет причины упоминать их в ответе пользователю)
        r"\b(handlers|guardrails|bot|llm|buffer|cache|alerts|config)\.py\b",
        # ----------------------------------------------------------------------
        # TODO (клиент): добавь сюда уникальные фразы из своего system.md.
        # Например, если в твоём промпте есть строка "Ты бот компании Acme",
        # добавь паттерн r"Ты бот компании Acme" — если LLM случайно
        # процитирует промпт пользователю, мы это заметим и заблокируем.
        # Чем уникальнее фраза-канарейка, тем меньше ложных срабатываний.
        # ----------------------------------------------------------------------
    ]
]


def check_output(text: str) -> tuple[bool, str | None]:
    """Проверить ответ LLM перед отправкой пользователю.

    Работает только при GUARDRAILS_OUTPUT_ENABLED=true.
    """
    if not GUARDRAILS_OUTPUT_ENABLED:
        return True, None
    for pattern in _OUTPUT_LEAK_PATTERNS:
        if pattern.search(text):
            return False, f"prompt_leak: {pattern.pattern}"
    return True, None
