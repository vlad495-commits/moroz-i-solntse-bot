from datetime import date
from decimal import Decimal

import pytest

from stats_calculations import (
    UsageRow,
    calculate_known_usage_cost,
    calculate_operator_estimate,
    parse_statistics_period,
)


def test_period_uses_inclusive_moscow_dates_and_utc_query_boundaries():
    period = parse_statistics_period(date(2026, 8, 1), date(2026, 8, 31))

    assert period.start_date == date(2026, 8, 1)
    assert period.end_date == date(2026, 8, 31)
    assert period.starts_at.isoformat() == "2026-07-31T21:00:00+00:00"
    assert period.ends_at.isoformat() == "2026-08-31T21:00:00+00:00"


def test_period_rejects_reversed_dates():
    with pytest.raises(ValueError, match="statistics period"):
        parse_statistics_period(date(2026, 8, 31), date(2026, 8, 1))


def test_known_model_cost_uses_recorded_model_and_cache_discount():
    result = calculate_known_usage_cost(
        [UsageRow("openai/gpt-4.1-mini", 1_000_000, 500_000, 250_000)]
    )

    assert result.cost_usd == Decimal("1.125")
    assert result.cache_savings_usd == Decimal("0.075")
    assert result.reason is None


def test_any_unknown_model_makes_whole_cost_unavailable():
    result = calculate_known_usage_cost(
        [
            UsageRow("gpt-4.1-mini", 100, 50, 0),
            UsageRow("gpt-5.6-luna", 100, 50, 0),
        ]
    )

    assert result.cost_usd is None
    assert result.cache_savings_usd is None
    assert result.reason == "Нет данных: для модели gpt-5.6-luna не задан тариф."


def test_operator_estimate_uses_explicit_formula():
    result = calculate_operator_estimate(3, Decimal("20"), Decimal("600"))

    assert result.hours == Decimal("1")
    assert result.savings_rub == Decimal("600")
    assert result.reason is None


def test_operator_estimate_requires_both_settings():
    result = calculate_operator_estimate(3, None, Decimal("600"))

    assert result.hours is None
    assert result.savings_rub is None
    assert result.reason == "Нет данных: заполните минуты оператора и ставку."
