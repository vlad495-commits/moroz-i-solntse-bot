from __future__ import annotations

import re
from dataclasses import dataclass
from typing import AbstractSet, Iterable

from moroz.security.pii import PLACEHOLDER_RE


INTERNAL_CANARY = "MOROZ_INTERNAL_CANARY_V1"

_PLACEHOLDER_SHAPE_RE = re.compile(r"<\s*PII(?:[_\s-][^>]*)?>", re.IGNORECASE)
_PRICE_RE = re.compile(
    r"(?<!\d)(?P<values>\d+(?:[ \u00a0]\d{3})*"
    r"(?:\s*/\s*\d+(?:[ \u00a0]\d{3})*)*)"
    r"\s*(?:руб(?:\.|лей?)?|₽)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"(?<![@\w])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}(?:/[a-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?",
    re.IGNORECASE,
)
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
_SLOT_RE = re.compile(
    r"(?P<date>\d{4}-\d{1,2}-\d{1,2}|"
    r"\d{1,2}\.\d{1,2}(?:\.\d{2,4})?|"
    r"сегодня|завтра|послезавтра)"
    r"\D{0,20}(?P<time>(?:[01]?\d|2[0-3]):[0-5]\d)",
    re.IGNORECASE,
)
_AVAILABILITY_RE = re.compile(
    r"\b(?:свободн\w*|доступн\w*|есть\s+окн\w*|"
    r"можно\s+записат\w*|available|open\s+slot)\b",
    re.IGNORECASE,
)
_NEGATED_AVAILABILITY_RULES = (
    re.compile(
        r"\b(?:нет|не\s+доступно)\s+"
        r"(?:свободн\w*|доступн\w*|окон\w*)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:свободн\w*|доступн\w*|окон\w*)\b.{0,50}\bнет\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:not\s+available|no\s+available\s+slots?)\b", re.IGNORECASE),
)
_NEGATED_PROMPT_LEAK_RULES = (
    re.compile(
        r"\b(?:не|не\s+могу|нельзя|cannot|can't|do\s+not|never)\s+"
        r"(?:раскры\w*|показ\w*|вывод\w*|reveal\w*|show\w*)"
        r".{0,40}\b(?:system\s+prompt|developer\s+instructions?|"
        r"системн\w*\s+промпт\w*|внутренн\w*\s+инструкц\w*)\b",
        re.IGNORECASE,
    ),
)
_PROMPT_LEAK_RULES = (
    re.compile(
        r"\b(?:system\s+prompt|developer\s+instructions?|"
        r"системн\w*\s+промпт\w*|внутренн\w*\s+инструкц\w*)"
        r"\s*:",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:вот|ниже|here(?:\s+are|'s)|below)\b.{0,50}\b"
        r"(?:system\s+prompt|developer\s+instructions?|"
        r"системн\w*\s+промпт\w*|внутренн\w*\s+инструкц\w*)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:system\s+prompt|developer\s+instructions?|"
        r"системн\w*\s+промпт\w*|внутренн\w*\s+инструкц\w*)\b"
        r".{0,30}\b(?:следующ\w*|таков\w*|below|follows?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:раскры\w*|показыва\w*|вывож\w*|reveal\w*|showing)\b"
        r".{0,40}\b(?:system\s+prompt|developer\s+instructions?|"
        r"системн\w*\s+промпт\w*|внутренн\w*\s+инструкц\w*)\b",
        re.IGNORECASE,
    ),
)
_NEGATED_MEDICAL_GUARANTEE_RULES = (
    re.compile(
        r"\b(?:не|нельзя|невозможно|not|cannot)\s+"
        r"(?:\w+\s+){0,2}(?:гарантир\w*|guarantee\w*)"
        r".{0,40}\b(?:результат\w*|эффект\w*|вылеч\w*|лечебн\w*|result|cure)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:без\s+гаранти\w*|no\s+guarantee\w*)"
        r".{0,40}\b(?:результат\w*|эффект\w*|result|cure)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:гаранти\w*|guarantee\w*)"
        r".{0,40}\b(?:результат\w*|эффект\w*|result|cure)\b"
        r".{0,20}\b(?:нет|нельзя|невозможно|not|cannot)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:результат\w*|эффект\w*|result|effect)\b"
        r".{0,15}\bне\s+навсегда\b",
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
        r"\b(?:гарантир\w*|гаранти[яиюей])\b.{0,40}\b"
        r"(?:лечебн\w*\s+)?(?:результат\w*|эффект\w*|"
        r"вылеч\w*|избав\w*|лечени\w*|выздоровл\w*)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bобязательн\w*\b.{0,30}\b"
        r"(?:вылеч\w*|избав\w*|исцел\w*|снимет\w*|"
        r"результат\w*|эффект\w*)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:вылеч\w*|избав\w*|исцел\w*|снимет\w*|"
        r"результат\w*|эффект\w*)\b"
        r".{0,40}\bнавсегда\b",
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


def _prices(text: str) -> frozenset[str]:
    return frozenset(
        re.sub(r"\D", "", value)
        for match in _PRICE_RE.finditer(text)
        for value in re.findall(r"\d+(?:[ \u00a0]\d{3})*", match["values"])
    )


def _normalize_slot(value: str) -> str:
    return " ".join(value.strip().split()).casefold()


def _normalize_date(value: str) -> str:
    value = value.casefold()
    if "-" in value:
        year, month, day = (int(part) for part in value.split("-"))
        return f"{year:04d}-{month:02d}-{day:02d}"
    if "." in value:
        parts = [int(part) for part in value.split(".")]
        return ".".join(f"{part:02d}" for part in parts)
    return value


def _slot_keys(text: str) -> frozenset[tuple[str, str]]:
    keys = frozenset(
        (
            _normalize_date(match["date"]),
            f"{int(match['time'].split(':')[0]):02d}:{match['time'][-2:]}",
        )
        for match in _SLOT_RE.finditer(text)
    )
    if keys:
        return keys
    return frozenset(
        ("", f"{int(match.group(0).split(':')[0]):02d}:{match.group(0)[-2:]}")
        for match in _TIME_RE.finditer(text)
    )


def _normalize_contact(value: str) -> str:
    value = value.rstrip(".,;:!?)]}»\"'/").casefold()
    if "@" in value:
        return value
    if "." in value:
        value = re.sub(r"^https?://", "", value)
        value = re.sub(r"^www\.", "", value).rstrip("/")
        return f"https://{value}"
    digits = "".join(char for char in value if char.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = f"7{digits[1:]}"
    elif len(digits) == 10 and digits.startswith("9"):
        digits = f"7{digits}"
    return f"+{digits}"


def _contacts(text: str) -> frozenset[str]:
    found = {
        _normalize_contact(match.group(0))
        for pattern in (_URL_RE, _EMAIL_RE, _HANDLE_RE, _DOMAIN_RE)
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


def _matches_after_removing(
    text: str,
    *,
    ignored: tuple[re.Pattern[str], ...],
    blocked: tuple[re.Pattern[str], ...],
) -> bool:
    for rule in ignored:
        text = rule.sub("", text)
    return any(rule.search(text) is not None for rule in blocked)


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
            price
            for source in sources
            for price in _prices(source)
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
    if INTERNAL_CANARY.casefold() in text.casefold():
        return ValidationVerdict(False, "prompt_leak")
    if _matches_after_removing(
        text,
        ignored=_NEGATED_PROMPT_LEAK_RULES,
        blocked=_PROMPT_LEAK_RULES,
    ):
        return ValidationVerdict(False, "prompt_leak")
    placeholders = set(_PLACEHOLDER_SHAPE_RE.findall(text))
    if (
        placeholders - set(allowed_placeholders)
        or placeholders - set(PLACEHOLDER_RE.findall(text))
    ):
        return ValidationVerdict(False, "unknown_placeholder")
    if _contacts(text) - facts.public_contacts:
        return ValidationVerdict(False, "new_raw_contact")
    if _matches_after_removing(
        text,
        ignored=_NEGATED_MEDICAL_GUARANTEE_RULES,
        blocked=_MEDICAL_GUARANTEE_RULES,
    ):
        return ValidationVerdict(False, "medical_guarantee")
    output_prices = _prices(text)
    if output_prices - facts.prices:
        return ValidationVerdict(False, "invented_price")
    if _matches_after_removing(
        text,
        ignored=_NEGATED_AVAILABILITY_RULES,
        blocked=(_AVAILABILITY_RE,),
    ):
        output_slots = _slot_keys(text)
        allowed_slots = frozenset(
            key for slot in facts.slots for key in _slot_keys(slot)
        )
        if not output_slots or output_slots - allowed_slots:
            return ValidationVerdict(False, "invented_slot")
    return ValidationVerdict(True, "output_valid")
