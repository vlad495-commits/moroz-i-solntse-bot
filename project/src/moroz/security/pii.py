from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import AbstractSet


PLACEHOLDER_RE = re.compile(r"<PII_[A-Z]+_\d+>")


class UnknownPlaceholder(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MaskedText:
    text: str
    mapping: Mapping[str, str]
    placeholders: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Rule:
    kind: str
    pattern: re.Pattern[str]
    value_group: str | None = None


_EMAIL_RE = re.compile(
    r"(?<![\w.+-])"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)
_PAYMENT_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_PHONE_RE = re.compile(r"(?<![\w])\+?(?:\d[\s().-]*){9,18}\d(?![\w])")
_NAME_RE = re.compile(
    r"(?P<prefix>\b(?:меня\s+зовут|имя|фио)\s*(?::|—|-)?\s*)"
    r"(?P<value>[А-ЯЁ][а-яё]+(?:[-\s][А-ЯЁ][а-яё]+){0,2})",
    re.IGNORECASE,
)
_ADDRESS_RE = re.compile(
    r"(?P<prefix>\b(?:адрес|место\s+жительства|улица|ул\.)"
    r"\s*(?::|—|-)?\s*)(?P<value>[^;\n]+)",
    re.IGNORECASE,
)
_HANDLE_RE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{5,32}\b")
_MEDICAL_RE = re.compile(
    r"(?P<prefix>\b(?:диагноз|анамнез|история\s+болезни|"
    r"медицинская\s+история)\s*(?::|—|-)?\s*)"
    r"(?P<value>[^;\n]+)",
    re.IGNORECASE,
)

_RULES = (
    _Rule("email", _EMAIL_RE),
    _Rule("payment", _PAYMENT_RE),
    _Rule("phone", _PHONE_RE),
    _Rule("name", _NAME_RE, "value"),
    _Rule("address", _ADDRESS_RE, "value"),
    _Rule("handle", _HANDLE_RE),
    _Rule("medical", _MEDICAL_RE, "value"),
)


def _passes_luhn(value: str) -> bool:
    digits = [int(char) for char in value if char.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _looks_like_phone(value: str) -> bool:
    return 10 <= sum(char.isdigit() for char in value) <= 15


class PiiSession:
    def __init__(self) -> None:
        self._mapping: dict[str, str] = {}
        self._reverse: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}

    def _placeholder(self, kind: str, value: str) -> str:
        key = (kind, value)
        if placeholder := self._reverse.get(key):
            return placeholder
        number = self._counters.get(kind, 0) + 1
        self._counters[kind] = number
        placeholder = f"<PII_{kind.upper()}_{number}>"
        self._mapping[placeholder] = value
        self._reverse[key] = placeholder
        return placeholder

    def mask(self, text: str) -> MaskedText:
        masked = text
        replacements: set[str] = set()

        for rule in _RULES:
            def replace(match: re.Match[str], *, current_rule: _Rule = rule) -> str:
                value = (
                    match.group(current_rule.value_group)
                    if current_rule.value_group
                    else match.group(0)
                )
                if current_rule.kind == "payment" and not _passes_luhn(value):
                    return match.group(0)
                if current_rule.kind == "phone" and not _looks_like_phone(value):
                    return match.group(0)
                placeholder = self._placeholder(current_rule.kind, value)
                replacements.add(placeholder)
                if current_rule.value_group:
                    return match.group("prefix") + placeholder
                return placeholder

            masked = rule.pattern.sub(replace, masked)

        present = frozenset(
            placeholder for placeholder in replacements if placeholder in masked
        )
        return MaskedText(
            text=masked,
            mapping=MappingProxyType(dict(self._mapping)),
            placeholders=present,
        )

    def restore_validated(
        self,
        text: str,
        allowed: AbstractSet[str],
    ) -> str:
        present = set(PLACEHOLDER_RE.findall(text))
        unknown = present - set(allowed)
        unknown.update(present - self._mapping.keys())
        if unknown:
            raise UnknownPlaceholder(tuple(sorted(unknown)))
        for placeholder in sorted(present, key=len, reverse=True):
            text = text.replace(placeholder, self._mapping[placeholder])
        return text


def find_raw_pii(text: str) -> frozenset[str]:
    placeholders = PiiSession().mask(text).placeholders
    return frozenset(
        placeholder.split("_", 2)[1].lower() for placeholder in placeholders
    )
