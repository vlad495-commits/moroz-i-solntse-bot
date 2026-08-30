from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from moroz.reactivation.policy import (
    MAIN_BUTTONS,
    EligibilityInput,
    ProgramPolicy,
    evaluate_eligibility,
    is_stop_request,
    next_send_at,
    template_checksum,
    validate_policy,
)


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def make_input(inactive_days: int = 90, **changes: object) -> EligibilityInput:
    values: dict[str, object] = {
        "identity_status": "verified",
        "consent_active": True,
        "consent_proven": True,
        "suppressed": False,
        "last_completed_visit_at": NOW - timedelta(days=inactive_days),
        "last_meaningful_inbound_at": None,
        "next_active_booking_at": None,
        "history_synced_at": NOW,
        "recent_bookings_synced_at": NOW,
        "sync_status": "current",
        "has_active_journey": False,
        "latest_journey_started_at": None,
        "human_mode": False,
        "has_open_escalation": False,
        "deletion_active": False,
    }
    values.update(changes)
    return EligibilityInput(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("inactive_days", "eligible", "reason"),
    [(89, False, "recent_activity"), (90, True, "eligible"), (91, True, "eligible")],
)
def test_inactivity_boundary(inactive_days: int, eligible: bool, reason: str) -> None:
    decision = evaluate_eligibility(
        make_input(inactive_days=inactive_days), ProgramPolicy(), NOW
    )

    assert (decision.eligible, decision.reason) == (eligible, reason)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"deletion_active": True}, "deletion"),
        ({"identity_status": "unverified"}, "no_verified_identity"),
        ({"identity_status": "conflict"}, "identity_conflict"),
        ({"identity_status": "unknown"}, "no_verified_identity"),
        ({"consent_proven": False}, "no_proven_consent"),
        ({"consent_active": False}, "consent_revoked"),
        ({"suppressed": True}, "suppressed"),
        ({"sync_status": "partial"}, "partial_sync"),
        ({"history_synced_at": NOW - timedelta(hours=24, seconds=1)}, "stale_history"),
        (
            {"recent_bookings_synced_at": NOW - timedelta(minutes=15, seconds=1)},
            "stale_recent_bookings",
        ),
        ({"next_active_booking_at": NOW + timedelta(days=1)}, "future_booking"),
        ({"has_active_journey": True}, "active_journey"),
        ({"latest_journey_started_at": NOW - timedelta(days=89)}, "cooldown"),
        ({"human_mode": True}, "human_mode"),
        ({"has_open_escalation": True}, "open_escalation"),
    ],
)
def test_every_safety_gate_excludes(change: dict[str, object], reason: str) -> None:
    decision = evaluate_eligibility(make_input(**change), ProgramPolicy(), NOW)

    assert (decision.eligible, decision.reason) == (False, reason)


def test_reason_priority_and_activity_anchor_use_the_latest_activity() -> None:
    value = make_input(
        deletion_active=True,
        identity_status="conflict",
        last_completed_visit_at=NOW - timedelta(days=120),
        last_meaningful_inbound_at=NOW - timedelta(days=89),
    )

    decision = evaluate_eligibility(value, ProgramPolicy(), NOW)

    assert decision.reason == "deletion"
    assert decision.activity_anchor_at == NOW - timedelta(days=89)


def test_missing_completed_visit_excludes_even_with_old_inbound() -> None:
    decision = evaluate_eligibility(
        make_input(
            last_completed_visit_at=None,
            last_meaningful_inbound_at=NOW - timedelta(days=120),
        ),
        ProgramPolicy(),
        NOW,
    )

    assert (decision.eligible, decision.reason) == (False, "no_completed_visit")


@pytest.mark.parametrize(
    ("local_now", "expected_utc"),
    [
        (datetime(2026, 8, 31, 7, 29, tzinfo=UTC), datetime(2026, 8, 31, 7, 30, tzinfo=UTC)),
        (datetime(2026, 8, 31, 7, 30, tzinfo=UTC), datetime(2026, 8, 31, 7, 30, tzinfo=UTC)),
        (datetime(2026, 8, 31, 17, 0, tzinfo=UTC), datetime(2026, 8, 31, 17, 0, tzinfo=UTC)),
        (datetime(2026, 8, 31, 17, 1, tzinfo=UTC), datetime(2026, 9, 1, 7, 30, tzinfo=UTC)),
    ],
)
def test_next_send_at_respects_moscow_quiet_time(
    local_now: datetime, expected_utc: datetime
) -> None:
    assert next_send_at(local_now) == expected_utc


@pytest.mark.parametrize(
    "policy",
    [
        ProgramPolicy(inactivity_days=89),
        ProgramPolicy(reminder_after_days=4),
        ProgramPolicy(cooldown_days=89),
    ],
)
def test_validate_policy_rejects_values_outside_the_fixed_allowlist(
    policy: ProgramPolicy,
) -> None:
    with pytest.raises(ValueError):
        validate_policy(policy)


def test_validate_policy_accepts_the_fixed_product_options() -> None:
    validate_policy(ProgramPolicy(inactivity_days=60, reminder_after_days=None, cooldown_days=120))


def test_template_checksum_is_stable_and_covers_policy_and_text() -> None:
    policy = ProgramPolicy()

    assert template_checksum(policy) == template_checksum(policy)
    assert template_checksum(replace(policy, main_text="Другой текст")) != template_checksum(policy)
    assert template_checksum(replace(policy, reminder_after_days=7)) != template_checksum(policy)


def test_template_checksum_uses_the_fixed_client_button_contract() -> None:
    assert MAIN_BUTTONS == (
        ("Записаться", "reactivation:book"),
        ("Задать вопрос", "reactivation:ask"),
        ("Не писать", "marketing:disable"),
    )


@pytest.mark.parametrize("text", ["стоп", "STOP", "не писать", "отписаться", "не присылайте"])
def test_stop_request_matches_only_allowlisted_full_phrases(text: str) -> None:
    assert is_stop_request(f"  {text.upper()}!!!  ") is True


@pytest.mark.parametrize(
    "text",
    ["можно стоп?", "не писать вам вопрос?", "почему не присылайте рекламу?", "отписаться от чего?"],
)
def test_stop_request_does_not_match_ordinary_questions(text: str) -> None:
    assert is_stop_request(text) is False


def test_eligibility_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_eligibility(
            make_input(history_synced_at=datetime(2026, 8, 31, 12, 0)), ProgramPolicy(), NOW
        )
