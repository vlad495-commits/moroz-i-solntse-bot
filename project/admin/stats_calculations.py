from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from pricing import PRICING_PER_1M


MOSCOW = ZoneInfo("Europe/Moscow")
MILLION = Decimal("1000000")


@dataclass(frozen=True, slots=True)
class StatisticsPeriod:
    start_date: date
    end_date: date
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class UsageRow:
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int


@dataclass(frozen=True, slots=True)
class CostResult:
    cost_usd: Decimal | None
    cache_savings_usd: Decimal | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class OperatorEstimate:
    hours: Decimal | None
    savings_rub: Decimal | None
    reason: str | None


def parse_statistics_period(start: date, end: date) -> StatisticsPeriod:
    if end < start:
        raise ValueError("statistics period")
    starts_at = datetime.combine(start, time.min, MOSCOW).astimezone(UTC)
    ends_at = datetime.combine(end + timedelta(days=1), time.min, MOSCOW).astimezone(
        UTC
    )
    return StatisticsPeriod(start, end, starts_at, ends_at)


def _known_pricing(model: str) -> dict | None:
    key = model.split("/")[-1].lower()
    return next(
        (prices for known, prices in PRICING_PER_1M.items() if known in key),
        None,
    )


def calculate_known_usage_cost(rows: list[UsageRow]) -> CostResult:
    cost = Decimal(0)
    savings = Decimal(0)
    for row in rows:
        prices = _known_pricing(row.model)
        if prices is None:
            return CostResult(
                None,
                None,
                f"Нет данных: для модели {row.model} не задан тариф.",
            )
        prompt_price = Decimal(str(prices["prompt"]))
        completion_price = Decimal(str(prices["completion"]))
        discount = Decimal(str(prices["cache_discount"]))
        cached = min(row.prompt_tokens, row.cached_tokens)
        fresh = max(0, row.prompt_tokens - cached)
        cache_price = prompt_price * (Decimal(1) - discount)
        cost += (
            Decimal(fresh) * prompt_price
            + Decimal(cached) * cache_price
            + Decimal(row.completion_tokens) * completion_price
        ) / MILLION
        savings += Decimal(cached) * (prompt_price - cache_price) / MILLION
    return CostResult(cost, savings, None)


def calculate_operator_estimate(
    automated_dialogues: int,
    minutes_per_dialogue: Decimal | None,
    hourly_rate_rub: Decimal | None,
) -> OperatorEstimate:
    if minutes_per_dialogue is None or hourly_rate_rub is None:
        return OperatorEstimate(
            None,
            None,
            "Нет данных: заполните минуты оператора и ставку.",
        )
    hours = Decimal(automated_dialogues) * minutes_per_dialogue / Decimal(60)
    return OperatorEstimate(hours, hours * hourly_rate_rub, None)
