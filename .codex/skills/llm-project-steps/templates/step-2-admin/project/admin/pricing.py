"""Расчёт стоимости использования LLM в долларах."""

# Цены в USD за 1М токенов. Актуальны на момент создания шаблона.
# Когда модели меняются — обновлять вручную.
PRICING_PER_1M = {
    "gpt-4.1-mini": {"prompt": 0.40, "completion": 1.60, "cache_discount": 0.75},
    "gpt-4.1": {"prompt": 2.00, "completion": 8.00, "cache_discount": 0.75},
    "gpt-4o-mini": {"prompt": 0.15, "completion": 0.60, "cache_discount": 0.50},
    "gpt-4o": {"prompt": 2.50, "completion": 10.00, "cache_discount": 0.50},
    "claude-haiku-4-5": {"prompt": 1.00, "completion": 5.00, "cache_discount": 0.90},
    "claude-sonnet-4-6": {"prompt": 3.00, "completion": 15.00, "cache_discount": 0.90},
    "claude-opus-4-7": {"prompt": 15.00, "completion": 75.00, "cache_discount": 0.90},
}

DEFAULT_PRICING = {"prompt": 0.40, "completion": 1.60, "cache_discount": 0.75}


def _model_pricing(model: str | None) -> dict:
    """Найти цены по модели (с учётом возможных префиксов типа 'openai/')."""
    if not model:
        return DEFAULT_PRICING
    key = model.split("/")[-1].lower()
    for known, prices in PRICING_PER_1M.items():
        if known in key:
            return prices
    return DEFAULT_PRICING


def calculate_cost(
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    model: str | None = None,
) -> tuple[float, float]:
    """Вернуть (cost_usd, savings_usd_from_cache).

    cost_usd — фактическая стоимость с учётом скидки на кэшированные токены.
    savings_usd — сэкономлено за счёт кэша (т.е. сколько бы стоило без кэша минус факт).
    """
    p = _model_pricing(model)

    fresh_prompt = max(0, prompt_tokens - cached_tokens)
    cache_price = p["prompt"] * (1 - p["cache_discount"])

    cost = (
        (fresh_prompt * p["prompt"]) / 1_000_000
        + (cached_tokens * cache_price) / 1_000_000
        + (completion_tokens * p["completion"]) / 1_000_000
    )

    # Savings: сколько стоили бы кэшированные токены БЕЗ кэша
    savings = (cached_tokens * (p["prompt"] - cache_price)) / 1_000_000

    return cost, savings
