from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import re
from typing import Literal
from unicodedata import category, normalize
from zoneinfo import ZoneInfo


DEFAULT_MAIN_TEXT = (
    "Здравствуйте! Вы давно не были в «Мороз и Солнце». "
    "Если захотите вернуться, я помогу подобрать процедуру и удобное время. "
    "Можно сразу начать запись или задать вопрос."
)
DEFAULT_REMINDER_TEXT = (
    "Ненавязчиво напомню: если захотите вернуться в «Мороз и Солнце», "
    "я помогу с выбором процедуры и записью. "
    "Если такие сообщения не нужны, нажмите «Не писать»."
)
REACTIVATION_RENDERER_VERSION = "reactivation-renderer-v1"
TELEGRAM_MESSAGE_MAX_LENGTH = 4096
_MOSCOW = ZoneInfo("Europe/Moscow")
_STOP_PHRASES = frozenset({"стоп", "stop", "не писать", "отписаться", "не присылайте"})
MAIN_BUTTONS = (
    ("Записаться", "reactivation:book"),
    ("Задать вопрос", "reactivation:ask"),
    ("Не писать", "marketing:disable"),
)
REMINDER_BUTTONS = MAIN_BUTTONS


@dataclass(frozen=True, slots=True)
class ProgramPolicy:
    inactivity_days: Literal[60, 90, 120] = 90
    reminder_after_days: Literal[3, 5, 7] | None = 5
    cooldown_days: int = 90
    main_text: str = DEFAULT_MAIN_TEXT
    reminder_text: str = DEFAULT_REMINDER_TEXT


@dataclass(frozen=True, slots=True)
class EligibilityInput:
    identity_status: Literal["unverified", "verified", "conflict"]
    consent_active: bool
    consent_proven: bool
    suppressed: bool
    last_completed_visit_at: datetime | None
    last_meaningful_inbound_at: datetime | None
    next_active_booking_at: datetime | None
    history_synced_at: datetime | None
    recent_bookings_synced_at: datetime | None
    sync_status: Literal["never", "current", "partial", "error"]
    has_active_journey: bool
    latest_journey_started_at: datetime | None
    human_mode: bool
    has_open_escalation: bool
    deletion_active: bool


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    eligible: bool
    reason: str
    activity_anchor_at: datetime | None


REASON_PRIORITY = (
    "deletion",
    "no_verified_identity",
    "identity_conflict",
    "no_proven_consent",
    "consent_revoked",
    "suppressed",
    "stale_history",
    "stale_recent_bookings",
    "partial_sync",
    "no_completed_visit",
    "recent_activity",
    "future_booking",
    "active_journey",
    "cooldown",
    "human_mode",
    "open_escalation",
)


def validate_policy(policy: ProgramPolicy) -> None:
    if policy.inactivity_days not in (60, 90, 120):
        raise ValueError("inactivity_days must be one of 60, 90, or 120")
    if policy.reminder_after_days not in (None, 3, 5, 7):
        raise ValueError("reminder_after_days must be None, 3, 5, or 7")
    if policy.cooldown_days < policy.inactivity_days:
        raise ValueError("cooldown_days must not be less than inactivity_days")
    if not policy.main_text.strip():
        raise ValueError("main_text must not be empty")
    if len(policy.main_text) > TELEGRAM_MESSAGE_MAX_LENGTH:
        raise ValueError("main_text exceeds Telegram message limit")
    if policy.reminder_after_days is not None:
        if not policy.reminder_text.strip():
            raise ValueError("reminder_text must not be empty when reminder is enabled")
        if len(policy.reminder_text) > TELEGRAM_MESSAGE_MAX_LENGTH:
            raise ValueError("reminder_text exceeds Telegram message limit")


def template_checksum(policy: ProgramPolicy) -> str:
    validate_policy(policy)
    payload = {
        "buttons": [
            {"label": label, "callback_data": callback_data}
            for label, callback_data in MAIN_BUTTONS
        ],
        "main_text": policy.main_text,
        "policy": {
            "cooldown_days": policy.cooldown_days,
            "inactivity_days": policy.inactivity_days,
            "reminder_after_days": policy.reminder_after_days,
        },
        "reminder_text": policy.reminder_text,
        "renderer_version": REACTIVATION_RENDERER_VERSION,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def evaluate_eligibility(
    value: EligibilityInput, policy: ProgramPolicy, now: datetime
) -> EligibilityDecision:
    validate_policy(policy)
    _validate_timestamps(value, now)
    anchor = _activity_anchor(value)

    checks = (
        (value.deletion_active, "deletion"),
        (value.identity_status not in ("verified", "conflict"), "no_verified_identity"),
        (value.identity_status == "conflict", "identity_conflict"),
        (not value.consent_proven, "no_proven_consent"),
        (not value.consent_active, "consent_revoked"),
        (value.suppressed, "suppressed"),
        (_is_stale(value.history_synced_at, now, timedelta(hours=24)), "stale_history"),
        (
            _is_stale(value.recent_bookings_synced_at, now, timedelta(minutes=15)),
            "stale_recent_bookings",
        ),
        (value.sync_status != "current", "partial_sync"),
        (value.last_completed_visit_at is None, "no_completed_visit"),
        (
            anchor is not None and now - anchor < timedelta(days=policy.inactivity_days),
            "recent_activity",
        ),
        (
            value.next_active_booking_at is not None and value.next_active_booking_at >= now,
            "future_booking",
        ),
        (value.has_active_journey, "active_journey"),
        (
            value.latest_journey_started_at is not None
            and now - value.latest_journey_started_at < timedelta(days=policy.cooldown_days),
            "cooldown",
        ),
        (value.human_mode, "human_mode"),
        (value.has_open_escalation, "open_escalation"),
    )
    for excluded, reason in checks:
        if excluded:
            return EligibilityDecision(False, reason, anchor)
    return EligibilityDecision(True, "eligible", anchor)


def next_send_at(now: datetime) -> datetime:
    _require_aware("now", now)
    local_now = now.astimezone(_MOSCOW)
    local_time = (local_now.hour, local_now.minute, local_now.second, local_now.microsecond)
    opening = (10, 30, 0, 0)
    closing = (20, 0, 0, 0)
    if local_time < opening:
        local_now = local_now.replace(hour=10, minute=30, second=0, microsecond=0)
    elif local_time > closing:
        local_now = (local_now + timedelta(days=1)).replace(
            hour=10, minute=30, second=0, microsecond=0
        )
    return local_now.astimezone(UTC)


def is_stop_request(text: str) -> bool:
    return _normalize_stop_command(text) in _STOP_PHRASES


def is_draft_stop_request(text: str) -> bool:
    return _normalize_stop_command(text) in {"стоп", "stop"}


def _normalize_stop_command(text: str) -> str:
    normalized = re.sub(r"\s+", " ", normalize("NFKC", text).lower()).strip()
    while normalized and category(normalized[-1]).startswith("P"):
        normalized = normalized[:-1].rstrip()
    return normalized


def _activity_anchor(value: EligibilityInput) -> datetime | None:
    timestamps = [
        timestamp
        for timestamp in (value.last_completed_visit_at, value.last_meaningful_inbound_at)
        if timestamp is not None
    ]
    return max(timestamps) if timestamps else None


def _is_stale(timestamp: datetime | None, now: datetime, max_age: timedelta) -> bool:
    return timestamp is None or now - timestamp > max_age


def _validate_timestamps(value: EligibilityInput, now: datetime) -> None:
    _require_aware("now", now)
    for name, timestamp in (
        ("last_completed_visit_at", value.last_completed_visit_at),
        ("last_meaningful_inbound_at", value.last_meaningful_inbound_at),
        ("next_active_booking_at", value.next_active_booking_at),
        ("history_synced_at", value.history_synced_at),
        ("recent_bookings_synced_at", value.recent_bookings_synced_at),
        ("latest_journey_started_at", value.latest_journey_started_at),
    ):
        if timestamp is not None:
            _require_aware(name, timestamp)


def _require_aware(name: str, timestamp: datetime) -> None:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
