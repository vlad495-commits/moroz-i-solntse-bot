import pytest

import llm as llm_module


@pytest.mark.asyncio
async def test_advertised_native_claude_provider_can_create_client():
    from anthropic import AsyncAnthropic

    client = llm_module._create_client("test-api-key", None, "anthropic")
    try:
        assert isinstance(client, AsyncAnthropic)
    finally:
        await client.close()
