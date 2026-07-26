from __future__ import annotations

import re
from dataclasses import dataclass
from typing import AbstractSet, Iterable

from moroz.security.pii import PLACEHOLDER_RE


INTERNAL_CANARY = "MOROZ_INTERNAL_CANARY_V1"

_ANY_PLACEHOLDER_RE = re.compile(r"<PII_[^>\s]*>")
_PRICE_RE = re.compile(
    r"(?<!\d)(\d+(?:[ \u00a0]\d{3})*)\s*(?:руб(?:\.|лей?)?|₽)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)
_HANDLE_RE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{3,32}\b")
_PHONE_RE = re.compile(r"(?<!\d)\+?(?:\d[ \t().-]*){9,18}\d(?!\d)")
_DATE_TIME_SHAPE_RE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}"
)
_TIME_RE = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)")
_AVAILABILITY_RE = re.compile(
    r"\b(?:свободн\w*|доступн\w*|есть\s+окн\w*|"
    r"можно\s+записат\w*|available|open\s+slot)\b",
    re.IGNORECASE,
)
_PROMPT_LEAK_RULES = (
    re.compile(re.escape(INTERNAL_CANARY), re.IGNORECASE),
    re.compile(
        r"\b(?:system\s+prompt|developer\s+instructions?|"
        r"системн\w*\s+промпт\w*|внутренн\w*\s+инструкц\w*)\b",
        re.IGNORECASE,
    ),
)
_MEDICAL_GUARANTEE_RULES = (
    re.compile(
        r"\b(?:гарантированно|точно|100\s*%)\s+"
        r"(?:вылечит\w*|избавит\w*|поможет\w*|снимет\w*)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:лечит|вылечит|исцеляет)\s+"
        r"(?:болезн\w*|заболеван\w*|диагноз\w*)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:guaranteed\s+to\s+(?:cure|heal)|"
        r"will\s+definitely\s+(?:cure|heal))\b",
        re.IGNORECASE,
    ),
)


def _normalize_price(value: str) -> str:
    match = re.search(r"\d+(?:[ \u00a0]\d{3})*", value)
    return "" if match is None else re.sub(r"\D", "", match.group(0))


def _normalize_slot(value: str) -> str:
    return " ".join(value.strip().split()).casefold()


def _normalize_contact(value: str) -> str:
    value = value.rstrip(".,;:!?)]}»\"'").casefold()
    if "@" in value or value.startswith(("http://", "https://")):
        return value
    digits = "".join(char for char in value if char.isdigit())
    return f"+{digits}" if value.lstrip().startswith("+") else digits


def _contacts(text: str) -> frozenset[str]:
    found = {
        _normalize_contact(match.group(0))
        for pattern in (_URL_RE, _EMAIL_RE, _HANDLE_RE)
        for match in pattern.finditer(text)
    }
    for match in _PHONE_RE.finditer(text):
        value = match.group(0).strip()
        digits = "".join(char for char in value if char.isdigit())
        if (
            10 <= len(digits) <= 15
            and _DATE_TIME_SHAPE_RE.fullmatch(value) is None
        ):
            found.add(_normalize_contact(value))
    return frozenset(found)


@dataclass(frozen=True, slots=True)
class StructuredFacts:
    prices: frozenset[str]
    public_contacts: frozenset[str]
    slots: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "prices",
            frozenset(
                normalized
                for price in self.prices
                if (normalized := _normalize_price(price))
            ),
        )
        object.__setattr__(
            self,
            "public_contacts",
            frozenset(_normalize_contact(value) for value in self.public_contacts),
        )
        object.__setattr__(
            self,
            "slots",
            frozenset(_normalize_slot(slot) for slot in self.slots if slot.strip()),
        )


@dataclass(frozen=True, slots=True)
class ValidationVerdict:
    ok: bool
    code: str


def extract_structured_facts(
    *sources: str,
    slots: Iterable[str] = (),
) -> StructuredFacts:
    return StructuredFacts(
        prices=frozenset(
            _normalize_price(match.group(0))
            for source in sources
            for match in _PRICE_RE.finditer(source)
        ),
        public_contacts=frozenset(
            contact for source in sources for contact in _contacts(source)
        ),
        slots=frozenset(slots),
    )


def validate_output(
    text: str,
    facts: StructuredFacts,
    allowed_placeholders: AbstractSet[str],
) -> ValidationVerdict:
    if not text.strip():
        return ValidationVerdict(False, "empty_output")
    if any(rule.search(text) is not None for rule in _PROMPT_LEAK_RULES):
        return ValidationVerdict(False, "prompt_leak")
    placeholders = set(_ANY_PLACEHOLDER_RE.findall(text))
    if (
        placeholders - set(allowed_placeholders)
        or placeholders - set(PLACEHOLDER_RE.findall(text))
    ):
        return ValidationVerdict(False, "unknown_placeholder")
    if _contacts(text) - facts.public_contacts:
        return ValidationVerdict(False, "new_raw_contact")
    if any(rule.search(text) is not None for rule in _MEDICAL_GUARANTEE_RULES):
        return ValidationVerdict(False, "medical_guarantee")
    output_prices = {
        _normalize_price(match.group(0)) for match in _PRICE_RE.finditer(text)
    }
    if output_prices - facts.prices:
        return ValidationVerdict(False, "invented_price")
    if _AVAILABILITY_RE.search(text):
        output_times = {match.group(0) for match in _TIME_RE.finditer(text)}
        allowed_times = {
            match.group(0)
            for slot in facts.slots
            for match in _TIME_RE.finditer(slot)
        }
        if output_times - allowed_times:
            return ValidationVerdict(False, "invented_slot")
    return ValidationVerdict(True, "output_valid")
