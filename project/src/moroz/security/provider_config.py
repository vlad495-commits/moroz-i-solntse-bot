"""Fail-closed resolution of optional dedicated LLM providers."""

from collections.abc import Mapping


ProviderTuple = tuple[str, str, str | None]


def resolve_provider_tuple(
    env: Mapping[str, str],
    prefix: str,
    parent: ProviderTuple,
) -> ProviderTuple:
    """Inherit the whole parent tuple or require a complete dedicated tuple."""
    model = env.get(f"{prefix}_MODEL", "")
    api_key = env.get(f"{prefix}_API_KEY", "")
    base_url = env.get(f"{prefix}_BASE_URL", "")
    if not (model or api_key or base_url):
        return parent
    parent_model, _parent_api_key, parent_base_url = parent
    if not api_key:
        same_model = not model or model == parent_model
        same_base_url = not base_url or base_url == parent_base_url
        if same_model and same_base_url:
            return parent
    if not model or not api_key:
        raise ValueError(
            f"{prefix}_API_KEY and {prefix}_MODEL must be set together"
        )
    return model, api_key, base_url or None
