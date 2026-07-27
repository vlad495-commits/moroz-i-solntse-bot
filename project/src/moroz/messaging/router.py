from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RouteDecision:
    intents: tuple[str, ...]
    requires_clarification: bool


_INTENT_RULES: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "complaint",
        (
            re.compile(
                r"\b(?:жалоб\w*|пожаловат\w*|недовол\w*|"
                r"верн\w*\s+деньг\w*|списал\w*\s+деньг\w*|"
                r"complaint|refund)\b",
                re.IGNORECASE,
            ),
        ),
    ),
    (
        "medical_risk",
        (
            re.compile(
                r"\b(?:сильн\w*\s+боль|резк\w*\s+ухудш\w*|обморок\w*|"
                r"потер\w*\s+сознани\w*|не\s+могу\s+дышать|"
                r"severe\s+pain|fainted|cannot\s+breathe)\b",
                re.IGNORECASE,
            ),
        ),
    ),
    (
        "booking_cancel",
        (
            re.compile(
                r"\b(?:отмен\w*|аннулир\w*|cancel)\b"
                r".{0,40}\b(?:запис\w*|визит\w*|booking|appointment)\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"\b(?:запис\w*|визит\w*|booking|appointment)\b"
                r".{0,40}\b(?:отмен\w*|аннулир\w*|cancel)\b",
                re.IGNORECASE,
            ),
        ),
    ),
    (
        "booking_change",
        (
            re.compile(
                r"\b(?:перенес\w*|перенос\w*|измен\w*|поменя\w*|"
                r"reschedul\w*|change)\b"
                r".{0,40}\b(?:запис\w*|визит\w*|врем\w*|день|"
                r"booking|appointment|time)\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"\b(?:запис\w*|визит\w*|booking|appointment)\b"
                r".{0,40}\b(?:перенес\w*|перенос\w*|измен\w*|"
                r"поменя\w*|reschedul\w*|change)\b",
                re.IGNORECASE,
            ),
        ),
    ),
    (
        "booking",
        (
            re.compile(
                r"\b(?:хочу|можно|нужно|как|давайте)?\s*"
                r"(?:записат\w*|запиш\w*|book)\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"\b(?:свободн\w*\s+(?:врем\w*|окн\w*)|"
                r"available\s+(?:time|slot))\b",
                re.IGNORECASE,
            ),
        ),
    ),
    (
        "faq",
        (
            re.compile(
                r"\b(?:сколько\s+стоит|цен\w*|прайс\w*|услуг\w*|"
                r"крио\w*|соляри\w*|коллари\w*|коллагенари\w*|"
                r"прессотерап\w*|массаж\w*|водородотерап\w*|"
                r"сертификат\w*|депозит\w*|адрес\w*|график\w*|"
                r"контакт\w*|телефон\w*|подготов\w*|"
                r"противопоказан\w*|faq|price|hours|address)\b",
                re.IGNORECASE,
            ),
        ),
    ),
)


def route_message(text: str) -> RouteDecision:
    intents = tuple(
        intent
        for intent, rules in _INTENT_RULES
        if any(rule.search(text) is not None for rule in rules)
    )
    if not intents:
        intents = ("unknown",)
    return RouteDecision(
        intents=intents,
        requires_clarification=(
            "booking_cancel" in intents and "booking_change" in intents
        ),
    )
