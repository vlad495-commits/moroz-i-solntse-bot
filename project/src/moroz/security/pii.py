from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import AbstractSet


PLACEHOLDER_RE = re.compile(r"<PII_[A-Z]+_\d+>")


class UnknownPlaceholder(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MaskedText:
    text: str
    mapping: Mapping[str, str] = field(repr=False)
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
_PHONE_RE = re.compile(r"(?<![\w])\+?(?:\d[ \t().-]*){9,18}\d(?![\w])")
_NON_PHONE_SHAPE_RE = re.compile(
    r"(?:\d{1,2}\.\d{1,2}\.\d{4}[ \t]+\d{1,2}(?:[.:]\d{2})?"
    r"|\d{1,6}\.\d{2}(?:[ \t]+\d{1,6}\.\d{2})+)"
)
_PHONE_MARKER_RE = re.compile(
    r"(?:телефон|номер(?:\s+телефона)?|тел\.)"
    r"(?:\s+для\s+(?:связи|записи))?\s*[:—-]?\s*$",
    re.IGNORECASE,
)
_NAME_RE = re.compile(
    r"(?P<prefix>\b(?:меня\s+зовут|имя|фио)\s*(?::|—|-)?\s*)"
    r"(?P<value>[А-ЯЁ][а-яё]+(?:[-\s][А-ЯЁ][а-яё]+){0,2})",
    re.IGNORECASE,
)
_QUESTION_TRANSITION = (
    r"(?:как\s+(?:добраться|записаться)|можно\s+ли|"
    r"что\s+(?:делать|выбрать)|где\s+(?:находится|записаться)|"
    r"когда\s+можно|сколько\s+стоит|есть\s+ли|подскажите)"
)
_SENSITIVE_VALUE_END = (
    rf"(?=;|\n|[,.]\s+{_QUESTION_TRANSITION}\b[^.;\n?]*\?|"
    r"[!?](?=\s+[А-ЯЁ]|$)|$)"
)
_ADDRESS_RE = re.compile(
    r"(?P<prefix>\b(?:адрес|место\s+жительства|улица|ул\.)"
    r"\s*(?::|—|-)?\s*)(?P<value>[^;\n]+?)"
    + _SENSITIVE_VALUE_END,
    re.IGNORECASE,
)
_HANDLE_RE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{3,32}\b")
_MEDICAL_RE = re.compile(
    r"(?P<prefix>\b(?:диагноз|анамнез|история\s+болезни|"
    r"медицинская\s+история)\s*(?::|—|-)?\s*)"
    r"(?P<value>[^;\n]+?)"
    + _SENSITIVE_VALUE_END,
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
_SPACE_PHONE_GROUPS = frozenset({(3, 3, 4), (4, 3, 4)})


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


def _looks_like_phone(value: str, leading_text: str) -> bool:
    if not 10 <= sum(char.isdigit() for char in value) <= 15:
        return False
    if _NON_PHONE_SHAPE_RE.fullmatch(value):
        return False
    groups = re.findall(r"\d+", value)
    group_shape = tuple(len(group) for group in groups)
    return (
        len(groups) == 1
        or any(char in value for char in "+()-")
        or (
            group_shape in _SPACE_PHONE_GROUPS
            and _PHONE_MARKER_RE.search(leading_text) is not None
        )
        or not all(len(group) >= 3 for group in groups)
    )


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
        source = PLACEHOLDER_RE.sub("[PII_TOKEN]", text)
        candidates: list[tuple[int, int, int, str, str]] = []

        for priority, rule in enumerate(_RULES):
            for match in rule.pattern.finditer(source):
                value = (
                    match.group(rule.value_group)
                    if rule.value_group
                    else match.group(0)
                )
                start, end = (
                    match.span(rule.value_group)
                    if rule.value_group
                    else match.span()
                )
                if rule.kind == "payment" and not _passes_luhn(value):
                    continue
                if rule.kind == "phone" and not _looks_like_phone(
                    value,
                    source[max(0, start - 40):start],
                ):
                    continue
                candidates.append((start, end, priority, rule.kind, value))

        selected: list[tuple[int, int, str, str]] = []
        selected_end = -1
        for start, end, _, kind, value in sorted(
            candidates,
            key=lambda item: (item[0], -(item[1] - item[0]), item[2]),
        ):
            if start < selected_end:
                continue
            selected.append((start, end, kind, value))
            selected_end = end

        parts: list[str] = []
        placeholders: set[str] = set()
        position = 0
        for start, end, kind, value in selected:
            placeholder = self._placeholder(kind, value)
            parts.extend((source[position:start], placeholder))
            placeholders.add(placeholder)
            position = end
        parts.append(source[position:])

        return MaskedText(
            text="".join(parts),
            mapping=MappingProxyType(dict(self._mapping)),
            placeholders=frozenset(placeholders),
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
