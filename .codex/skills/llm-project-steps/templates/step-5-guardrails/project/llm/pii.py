"""
PII masking перед отправкой в зарубежный LLM (152-ФЗ).

Слой 1 (всегда работает, если PII_MASK_ENABLED=true):
    Regex + контрольные суммы — паспорт, СНИЛС, ИНН (10/12),
    банковская карта (Луна), полис ОМС, телефон РФ, email, IP,
    дата рождения, свидетельство о рождении.

Слой 2 (опционально, PII_NER_ENABLED=true):
    Presidio + spaCy ru_core_news_lg — имена, локации, организации
    в свободном тексте. Требует установки `presidio-analyzer` и модели
    `ru_core_news_lg` в Docker-образе (см. UPGRADE.md ступени 5).

Mapping `<LABEL_N>` → оригинал возвращается из mask_pii() для restore_pii().
"""
import logging
import re
import threading
from collections import defaultdict
from typing import Callable, Optional

from config import PII_MASK_ENABLED, PII_NER_ENABLED

logger = logging.getLogger(__name__)


# === Валидаторы контрольных сумм ===

def _validate_snils(value: str) -> bool:
    digits = "".join(c for c in value if c.isdigit())
    if len(digits) != 11:
        return False
    checksum = sum(int(digits[i]) * (9 - i) for i in range(9))
    if checksum > 101:
        checksum %= 101
    if checksum in (100, 101):
        checksum = 0
    return checksum == int(digits[9:11])


def _validate_inn_10(value: str) -> bool:
    if len(value) != 10 or not value.isdigit():
        return False
    coeffs = [2, 4, 10, 3, 5, 9, 4, 6, 8]
    checksum = sum(int(value[i]) * coeffs[i] for i in range(9)) % 11 % 10
    return checksum == int(value[9])


def _validate_inn_12(value: str) -> bool:
    if len(value) != 12 or not value.isdigit():
        return False
    c1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    c2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    d10 = sum(int(value[i]) * c1[i] for i in range(10)) % 11 % 10
    d11 = sum(int(value[i]) * c2[i] for i in range(11)) % 11 % 10
    return d10 == int(value[10]) and d11 == int(value[11])


def _luhn(value: str) -> bool:
    digits = [int(c) for c in value if c.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    odd = digits[-1::-2]
    even = digits[-2::-2]
    total = sum(odd) + sum(sum(divmod(d * 2, 10)) for d in even)
    return total % 10 == 0


def _validate_oms(value: str) -> bool:
    digits = [int(c) for c in value if c.isdigit()]
    if len(digits) != 16:
        return False
    total = 0
    for i, d in enumerate(reversed(digits[:-1])):
        if i % 2 == 0:
            doubled = d * 2
            total += doubled // 10 + doubled % 10
        else:
            total += d
    check = (10 - total % 10) % 10
    return check == digits[15]


# === Regex-паттерны (порядок важен — сначала специфичные) ===

_PATTERNS: list[tuple[str, str, Optional[Callable[[str], bool]]]] = [
    # СНИЛС: 123-456-789 01
    (r"\b\d{3}-\d{3}-\d{3}\s\d{2}\b", "SNILS", _validate_snils),
    # Свидетельство о рождении: I-ЕА 720345 (римская серия + 2 кириллические + 6 цифр)
    (r"\b[IVX]{1,4}-[А-Я]{2}\s?\d{6}\b", "BIRTH_CERT", None),
    # Паспорт РФ: 1234 567890
    (r"\b\d{4}\s\d{6}\b", "PASSPORT_RU", None),
    # Полис ОМС: 16 цифр БЕЗ разделителей (Mod10).
    # Идёт ПЕРЕД CARD — у них совпадает алгоритм контрольной суммы (Mod10/Луна),
    # 16-значный номер без пробелов считаем ОМС.
    (r"\b\d{16}\b", "OMS", _validate_oms),
    # Банковская карта (Луна): 13-19 цифр с пробелами/дефисами между группами.
    (r"\b(?:\d[ \-]?){12,18}\d\b", "CARD", _luhn),
    # ИНН физлица (12 цифр)
    (r"\b\d{12}\b", "INN_PERSON", _validate_inn_12),
    # ИНН юрлица (10 цифр)
    (r"\b\d{10}\b", "INN_ORG", _validate_inn_10),
    # Телефон РФ: +7/8, со скобками/пробелами/дефисами.
    # (?<!\d)...(?!\d) — чтобы не матчился внутри длинной цепочки цифр
    # (например невалидный полис ОМС). \b не подходит из-за +.
    (
        r"(?<!\d)(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)",
        "PHONE_RU",
        None,
    ),
    # Email
    (r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", "EMAIL", None),
    # IPv4
    (
        r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
        "IP",
        None,
    ),
    # Дата рождения (1900-2029)
    (
        r"\b(?:0[1-9]|[12]\d|3[01])[.\-/](?:0[1-9]|1[0-2])[.\-/](?:19\d{2}|20[0-2]\d)\b",
        "BIRTHDATE",
        None,
    ),
]

_PLACEHOLDER_RE = re.compile(r"<[A-Z_]+_\d+>")


# === NER (опционально через Presidio + spaCy) ===

_analyzer = None
_ner_lock = threading.Lock()
_ner_init_failed = False
_NER_ENTITIES = ("PERSON", "LOCATION", "ORGANIZATION")


def _init_ner() -> bool:
    """Ленивая инициализация Presidio + spaCy. Возвращает True если успешно."""
    global _analyzer, _ner_init_failed
    if _analyzer is not None:
        return True
    if _ner_init_failed:
        return False
    with _ner_lock:
        if _analyzer is not None:
            return True
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider

            configuration = {
                "nlp_engine_name": "spacy",
                "models": [
                    {"lang_code": "ru", "model_name": "ru_core_news_lg"},
                ],
            }
            provider = NlpEngineProvider(nlp_configuration=configuration)
            nlp_engine = provider.create_engine()
            _analyzer = AnalyzerEngine(
                nlp_engine=nlp_engine,
                supported_languages=["ru"],
            )
            logger.info("PII NER загружен (Presidio + spaCy ru_core_news_lg)")
            return True
        except Exception as e:
            _ner_init_failed = True
            logger.error(
                "PII NER не загружен: %s. Проверь requirements.txt и Dockerfile.",
                e,
            )
            return False


# === Публичные функции ===

def mask_pii(text: str) -> tuple[str, dict[str, str]]:
    """
    Маскирует PII в тексте.
    Возвращает (маскированный текст, mapping {placeholder: оригинал}).

    Если PII_MASK_ENABLED=false — возвращает текст без изменений.
    """
    if not PII_MASK_ENABLED or not text:
        return text, {}

    counter: dict[str, int] = defaultdict(int)
    mapping: dict[str, str] = {}
    masked = text

    # --- Слой 1: regex с валидаторами ---
    for pattern, label, validator in _PATTERNS:
        matches: list[tuple[int, int, str]] = []
        for m in re.finditer(pattern, masked):
            value = m.group()
            # Skip если value — уже placeholder или пересекается с ним
            if _PLACEHOLDER_RE.search(value):
                continue
            if validator and not validator(value):
                continue
            matches.append((m.start(), m.end(), value))
        # Заменяем с конца, чтобы индексы не сдвигались
        for start, end, value in reversed(matches):
            counter[label] += 1
            placeholder = f"<{label}_{counter[label]}>"
            mapping[placeholder] = value
            masked = masked[:start] + placeholder + masked[end:]

    # --- Слой 2: NER (опционально) ---
    if PII_NER_ENABLED and _init_ner():
        try:
            results = _analyzer.analyze(
                text=masked,
                language="ru",
                entities=list(_NER_ENTITIES),
            )
            for r in sorted(results, key=lambda x: -x.start):
                value = masked[r.start:r.end]
                if _PLACEHOLDER_RE.search(value):
                    continue
                counter[r.entity_type] += 1
                placeholder = f"<{r.entity_type}_{counter[r.entity_type]}>"
                mapping[placeholder] = value
                masked = masked[:r.start] + placeholder + masked[r.end:]
        except Exception as e:
            logger.exception("Ошибка PII NER при анализе: %s", e)

    return masked, mapping


def restore_pii(text: str, mapping: dict[str, str]) -> str:
    """Восстанавливает оригинальные значения по mapping."""
    if not mapping or not text:
        return text
    # Сначала длинные placeholder, чтобы <PERSON_10> заменился раньше <PERSON_1>
    for placeholder in sorted(mapping.keys(), key=len, reverse=True):
        text = text.replace(placeholder, mapping[placeholder])
    return text
