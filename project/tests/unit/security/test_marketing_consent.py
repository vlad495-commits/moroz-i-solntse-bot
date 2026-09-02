from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
import importlib

import pytest

import config as llm_config

from moroz.security.consent import (
    MARKETING_CONSENT_VERSION,
    ConsentService,
    MarketingConsentState,
)


class DatabaseMustNotBeUsed:
    def acquire(self):
        raise AssertionError("invalid grant reached the database")


def test_marketing_consent_state_is_frozen_and_versioned():
    state = MarketingConsentState(
        consent_id=None,
        active=False,
        consent_version=None,
        proof_text_hash=None,
        source=None,
        source_event_id=None,
        suppressed=False,
        suppression_reason=None,
    )

    assert MARKETING_CONSENT_VERSION == "marketing-v1"
    with pytest.raises(FrozenInstanceError):
        state.active = True


@pytest.mark.asyncio
async def test_admin_source_cannot_grant_marketing_consent():
    service = ConsentService(DatabaseMustNotBeUsed())

    with pytest.raises(ValueError, match="explicit Telegram action"):
        await service.grant_marketing(
            channel="telegram",
            user_id="42",
            proof_text="Точный текст согласия",
            source="admin",
            source_event_id="admin-1",
            occurred_at=datetime(2026, 8, 31, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_marketing_event_requires_timezone_aware_timestamp():
    service = ConsentService(DatabaseMustNotBeUsed())

    with pytest.raises(ValueError, match="timezone-aware"):
        await service.revoke_marketing(
            channel="telegram",
            user_id="42",
            source="telegram_explicit",
            source_event_id="103",
            occurred_at=datetime(2026, 8, 31),
        )


def test_consent_prompt_env_cannot_replace_canonical_marketing_clause(
    monkeypatch,
):
    monkeypatch.setenv("CONSENT_PROMPT", "Подменённый текст без условий")
    overridden = importlib.reload(llm_config)
    try:
        assert "Подменённый текст" not in overridden.CONSENT_PROMPT
        assert overridden.MARKETING_CONSENT_CLAUSE in overridden.CONSENT_PROMPT
        assert "{policy_url}" in overridden.CONSENT_PROMPT
    finally:
        monkeypatch.delenv("CONSENT_PROMPT")
        importlib.reload(llm_config)
