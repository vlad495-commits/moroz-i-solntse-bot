import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Literal
from uuid import UUID, uuid4

from moroz.booking.models import ExternalBooking
from moroz.common.db import Database


WorkflowKind = Literal["create", "reschedule", "cancel"]
_ACTIVE_PHASES = ("collecting", "awaiting_confirmation", "executing")
_CONFIRM_RECOVERY_PHASES = ("collecting", "executing", "confirmed", "escalated")
_LEGACY_OWNERSHIP_SENTINELS = frozenset({"__legacy__", "unknown", "missing"})


class WorkflowRevisionConflict(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("value is not JSON-compatible")


def _json_object(value: object, field: str) -> Mapping[str, object]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, Mapping):
        raise ValueError(f"{field} must be a JSON object")
    return _freeze_json(decoded)


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("value is not JSON-compatible")


def _dump_json_object(value: Mapping[str, object], field: str) -> str:
    frozen = _json_object(value, field)
    return json.dumps(_thaw_json(frozen), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class WorkflowSession:
    id: UUID
    kind: WorkflowKind
    phase: str
    idempotency_key: str
    customer_id: str
    channel: str
    chat_id: str
    revision: int
    state: Mapping[str, object]
    error_code: str | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        if self.expires_at is not None:
            _require_aware(self.expires_at)
        _require_aware(self.created_at)
        _require_aware(self.updated_at)
        object.__setattr__(self, "state", _json_object(self.state, "state"))


@dataclass(frozen=True, slots=True)
class BookingAction:
    id: str
    scenario_id: UUID
    customer_id: str
    channel: str
    chat_id: str
    revision: int
    action_kind: str
    payload: Mapping[str, object]
    expires_at: datetime
    consumed_at: datetime | None
    result: Mapping[str, object] | None

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        _require_aware(self.expires_at)
        if self.consumed_at is not None:
            _require_aware(self.consumed_at)
        object.__setattr__(self, "payload", _json_object(self.payload, "payload"))
        if self.result is not None:
            object.__setattr__(self, "result", _json_object(self.result, "result"))


@dataclass(frozen=True, slots=True)
class ActionCompletion:
    session: WorkflowSession
    result: Mapping[str, object]
    replayed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "result", _json_object(self.result, "result"))


class BookingWorkflowRepository:
    def __init__(
        self,
        database: Database,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._database = database
        self._now = now

    async def start(
        self,
        kind: WorkflowKind,
        channel: str,
        chat_id: str,
        customer_id: str,
        idempotency_key: str,
    ) -> WorkflowSession:
        now = self._now()
        _require_aware(now)
        identity_lock = f"booking:workflow:{channel}:{chat_id}:{customer_id}"
        async with self._database.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    identity_lock,
                )
                row = await connection.fetchrow(
                    """
                    SELECT * FROM booking_scenarios
                    WHERE channel = $1 AND chat_id = $2 AND customer_id = $3
                      AND phase = ANY($4::text[])
                    ORDER BY created_at, id
                    LIMIT 1
                    FOR UPDATE
                    """,
                    channel,
                    chat_id,
                    customer_id,
                    list(_ACTIVE_PHASES),
                )
                if row is not None:
                    return self._session_from_row(row)
                idempotent = await connection.fetchrow(
                    """
                    SELECT * FROM booking_scenarios
                    WHERE idempotency_key = $1
                    FOR UPDATE
                    """,
                    idempotency_key,
                )
                if idempotent is not None:
                    if (
                        idempotent["channel"] != channel
                        or idempotent["chat_id"] != chat_id
                        or idempotent["customer_id"] != customer_id
                    ):
                        raise RuntimeError("workflow idempotency conflict")
                    return self._session_from_row(idempotent)
                scenario_id = uuid4()
                row = await connection.fetchrow(
                    """
                    INSERT INTO booking_scenarios
                        (id, kind, phase, idempotency_key, customer_id, state,
                         error_code, channel, chat_id, revision, expires_at,
                         created_at, updated_at)
                    VALUES
                        ($1, $2, 'collecting', $3, $4, '{}'::jsonb,
                         NULL, $5, $6, 0, NULL, $7, $7)
                    RETURNING *
                    """,
                    scenario_id,
                    kind,
                    idempotency_key,
                    customer_id,
                    channel,
                    chat_id,
                    now,
                )
                await self._insert_event(
                    connection,
                    scenario_id,
                    "booking_workflow_started",
                    {},
                    now,
                )
        return self._session_from_row(row)

    async def get_active(
        self,
        channel: str,
        chat_id: str,
        customer_id: str,
    ) -> WorkflowSession | None:
        async with self._database.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM booking_scenarios
                WHERE channel = $1 AND chat_id = $2 AND customer_id = $3
                  AND phase = ANY($4::text[])
                ORDER BY created_at, id
                LIMIT 1
                """,
                channel,
                chat_id,
                customer_id,
                list(_ACTIVE_PHASES),
            )
        return self._session_from_row(row) if row is not None else None

    async def get(self, scenario_id: UUID) -> WorkflowSession | None:
        async with self._database.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM booking_scenarios WHERE id = $1",
                scenario_id,
            )
        return self._session_from_row(row) if row is not None else None

    async def checkpoint(
        self,
        session: WorkflowSession,
        event_type: str,
        payload: Mapping[str, object] | None = None,
        *,
        action_id: str | None = None,
        result: Mapping[str, object] | None = None,
    ) -> WorkflowSession:
        if (action_id is None) != (result is None):
            raise ValueError("action_id and result must be provided together")
        state_json = _dump_json_object(session.state, "state")
        payload_json = _dump_json_object(payload or {}, "payload")
        result_json = (
            _dump_json_object(result, "result") if result is not None else None
        )
        now = self._now()
        _require_aware(now)
        async with self._database.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    "SELECT * FROM booking_scenarios WHERE id = $1 FOR UPDATE",
                    session.id,
                )
                if row is None:
                    raise KeyError(f"booking scenario {session.id} not found")
                action_row = None
                if action_id is not None:
                    action_row = await connection.fetchrow(
                        """
                        SELECT * FROM booking_actions
                        WHERE id = $1
                        FOR UPDATE
                        """,
                        action_id,
                    )
                    if (
                        action_row is None
                        or action_row["scenario_id"] != session.id
                        or action_row["channel"] != row["channel"]
                        or action_row["chat_id"] != row["chat_id"]
                        or action_row["customer_id"] != row["customer_id"]
                    ):
                        raise RuntimeError("booking action conflict")
                    if action_row["consumed_at"] is not None:
                        saved = _json_object(action_row["result"], "result")
                        expected = _json_object(result, "result")
                        if saved != expected:
                            raise RuntimeError("booking action result conflict")
                        return self._session_from_row(row)
                    if (
                        action_row["revision"] != row["revision"]
                        or action_row["expires_at"] <= now
                    ):
                        raise RuntimeError("booking action is stale")
                if row["revision"] != session.revision:
                    raise WorkflowRevisionConflict("workflow revision conflict")
                updated = await connection.fetchrow(
                    """
                    UPDATE booking_scenarios
                    SET phase = $2, state = $3::jsonb, error_code = $4,
                        expires_at = $5, revision = revision + 1,
                        updated_at = $6
                    WHERE id = $1 AND revision = $7
                    RETURNING *
                    """,
                    session.id,
                    session.phase,
                    state_json,
                    session.error_code,
                    session.expires_at,
                    now,
                    session.revision,
                )
                if updated is None:
                    raise WorkflowRevisionConflict("workflow revision conflict")
                await self._insert_event(
                    connection,
                    session.id,
                    event_type,
                    payload_json,
                    now,
                    encoded=True,
                )
                if action_row is not None:
                    status = await connection.execute(
                        """
                        UPDATE booking_actions
                        SET consumed_at = $2, result = $3::jsonb
                        WHERE id = $1 AND consumed_at IS NULL
                        """,
                        action_id,
                        now,
                        result_json,
                    )
                    if status != "UPDATE 1":
                        raise RuntimeError("booking action conflict")
        return self._session_from_row(updated)

    async def issue_action(
        self,
        scenario_id: UUID,
        revision: int,
        action_kind: str,
        payload: Mapping[str, object],
        expires_at: datetime,
    ) -> BookingAction:
        _require_aware(expires_at)
        payload_json = _dump_json_object(payload, "payload")
        async with self._database.acquire() as connection:
            async with connection.transaction():
                scenario = await connection.fetchrow(
                    "SELECT * FROM booking_scenarios WHERE id = $1 FOR UPDATE",
                    scenario_id,
                )
                if scenario is None:
                    raise KeyError(f"booking scenario {scenario_id} not found")
                if scenario["revision"] != revision:
                    raise WorkflowRevisionConflict("workflow revision conflict")
                if not all(
                    isinstance(scenario[field], str) and scenario[field]
                    for field in ("customer_id", "channel", "chat_id")
                ):
                    raise RuntimeError("workflow identity is incomplete")
                if expires_at <= self._now():
                    raise ValueError("action expiry must be in the future")
                for _ in range(3):
                    action_id = secrets.token_urlsafe(12)
                    row = await connection.fetchrow(
                        """
                        INSERT INTO booking_actions
                            (id, scenario_id, customer_id, channel, chat_id,
                             revision, action_kind, payload, expires_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
                        ON CONFLICT (id) DO NOTHING
                        RETURNING *
                        """,
                        action_id,
                        scenario_id,
                        scenario["customer_id"],
                        scenario["channel"],
                        scenario["chat_id"],
                        revision,
                        action_kind,
                        payload_json,
                        expires_at,
                    )
                    if row is None:
                        continue
                    return self._action_from_row(row)
        raise RuntimeError("could not allocate booking action id")

    async def consume_action(
        self,
        action_id: str,
        channel: str,
        chat_id: str,
        customer_id: str,
    ) -> BookingAction | None:
        async with self._database.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT a.*, s.revision AS scenario_revision,
                           s.phase AS scenario_phase
                    FROM booking_actions AS a
                    JOIN booking_scenarios AS s ON s.id = a.scenario_id
                    WHERE a.id = $1
                    FOR UPDATE OF a
                    """,
                    action_id,
                )
                if row is None:
                    return None
                if (
                    row["channel"] != channel
                    or row["chat_id"] != chat_id
                    or row["customer_id"] != customer_id
                ):
                    return None
                if row["consumed_at"] is not None and row["result"] is not None:
                    return self._action_from_row(row)
                recoverable_confirm = (
                    row["action_kind"] == "confirm"
                    and row["scenario_phase"] in _CONFIRM_RECOVERY_PHASES
                )
                if row["revision"] != row["scenario_revision"] or (
                    row["expires_at"] <= self._now()
                    and not recoverable_confirm
                ):
                    return None
                return self._action_from_row(row)

    async def complete_action(
        self,
        action_id: str,
        channel: str,
        chat_id: str,
        customer_id: str,
        result: Mapping[str, object],
        event_type: str,
        payload: Mapping[str, object] | None = None,
        *,
        recovery_session: WorkflowSession | None = None,
    ) -> ActionCompletion:
        result_object = _json_object(result, "result")
        result_json = _dump_json_object(result_object, "result")
        payload_json = _dump_json_object(payload or {}, "payload")
        now = self._now()
        _require_aware(now)
        async with self._database.acquire() as connection:
            scenario_id = await connection.fetchval(
                "SELECT scenario_id FROM booking_actions WHERE id = $1",
                action_id,
            )
            if scenario_id is None:
                raise RuntimeError("booking action conflict")
            async with connection.transaction():
                scenario = await connection.fetchrow(
                    "SELECT * FROM booking_scenarios WHERE id = $1 FOR UPDATE",
                    scenario_id,
                )
                if scenario is None:
                    raise RuntimeError("booking action conflict")
                action = await connection.fetchrow(
                    "SELECT * FROM booking_actions WHERE id = $1 FOR UPDATE",
                    action_id,
                )
                if (
                    action is None
                    or action["scenario_id"] != scenario["id"]
                    or (
                        action["channel"],
                        action["chat_id"],
                        action["customer_id"],
                    )
                    != (channel, chat_id, customer_id)
                    or (
                        scenario["channel"],
                        scenario["chat_id"],
                        scenario["customer_id"],
                    )
                    != (channel, chat_id, customer_id)
                ):
                    raise RuntimeError("booking action conflict")
                if action["consumed_at"] is not None:
                    saved = _json_object(action["result"], "result")
                    if saved != result_object:
                        raise RuntimeError("booking action result conflict")
                    return ActionCompletion(
                        self._session_from_row(scenario),
                        saved,
                        True,
                    )
                recoverable_confirm = (
                    action["action_kind"] == "confirm"
                    and scenario["phase"] in _CONFIRM_RECOVERY_PHASES
                )
                if action["revision"] != scenario["revision"] or (
                    action["expires_at"] <= now
                    and not recoverable_confirm
                ):
                    raise RuntimeError("booking action is stale")
                if recovery_session is not None:
                    if (
                        action["action_kind"] != "confirm"
                        or scenario["phase"] != "collecting"
                        or recovery_session.id != scenario["id"]
                        or recovery_session.revision != scenario["revision"]
                        or recovery_session.phase != "collecting"
                        or (
                            recovery_session.channel,
                            recovery_session.chat_id,
                            recovery_session.customer_id,
                        )
                        != (channel, chat_id, customer_id)
                    ):
                        raise RuntimeError("booking action recovery conflict")
                    recovery_state = _dump_json_object(
                        recovery_session.state,
                        "state",
                    )
                    updated = await connection.fetchrow(
                        """
                        UPDATE booking_scenarios
                        SET phase = 'collecting', state = $2::jsonb,
                            error_code = $3, expires_at = $4,
                            revision = revision + 1, updated_at = $5
                        WHERE id = $1 AND revision = $6
                        RETURNING *
                        """,
                        scenario["id"],
                        recovery_state,
                        recovery_session.error_code,
                        recovery_session.expires_at,
                        now,
                        scenario["revision"],
                    )
                else:
                    updated = await connection.fetchrow(
                        """
                        UPDATE booking_scenarios
                        SET revision = revision + 1, updated_at = $2
                        WHERE id = $1 AND revision = $3
                        RETURNING *
                        """,
                        scenario["id"],
                        now,
                        scenario["revision"],
                    )
                if updated is None:
                    raise WorkflowRevisionConflict("workflow revision conflict")
                await self._insert_event(
                    connection,
                    scenario["id"],
                    event_type,
                    payload_json,
                    now,
                    encoded=True,
                )
                status = await connection.execute(
                    """
                    UPDATE booking_actions
                    SET consumed_at = $2, result = $3::jsonb
                    WHERE id = $1 AND consumed_at IS NULL
                    """,
                    action_id,
                    now,
                    result_json,
                )
                if status != "UPDATE 1":
                    raise RuntimeError("booking action conflict")
        return ActionCompletion(
            self._session_from_row(updated),
            result_object,
            False,
        )

    async def list_owned_active_bookings(
        self,
        customer_id: str,
    ) -> list[ExternalBooking]:
        async with self._database.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT external_id, customer_id, booking_key, slot_id,
                       starts_at, scheduled_end_at, status, snapshot
                FROM bookings
                WHERE customer_id = $1 AND status = 'confirmed'
                  AND starts_at > $2
                ORDER BY starts_at, id
                """,
                customer_id,
                self._now(),
            )
        bookings: list[ExternalBooking] = []
        for row in rows:
            try:
                snapshot = _json_object(row["snapshot"], "snapshot")
            except ValueError:
                continue
            raw_service_ids = snapshot.get("service_ids")
            raw_staff_id = snapshot.get("staff_id")
            if (
                not isinstance(raw_service_ids, tuple)
                or not raw_service_ids
                or not all(isinstance(item, str) and item for item in raw_service_ids)
                or not isinstance(raw_staff_id, str)
                or not raw_staff_id
                or any(
                    item.casefold() in _LEGACY_OWNERSHIP_SENTINELS
                    for item in raw_service_ids
                )
                or raw_staff_id.casefold() in _LEGACY_OWNERSHIP_SENTINELS
            ):
                continue
            bookings.append(
                ExternalBooking(
                    external_id=row["external_id"],
                    customer_id=row["customer_id"],
                    booking_key=row["booking_key"],
                    slot_id=row["slot_id"],
                    service_ids=raw_service_ids,
                    staff_id=raw_staff_id,
                    starts_at=row["starts_at"],
                    status=row["status"],
                    scheduled_end_at=row["scheduled_end_at"],
                )
            )
        return bookings

    async def is_human_mode(self, customer_id: str) -> bool:
        async with self._database.acquire() as connection:
            return bool(
                await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM human_mode
                        WHERE customer_id = $1 AND enabled = true
                          AND (expires_at IS NULL OR expires_at > $2)
                    )
                    """,
                    customer_id,
                    self._now(),
                )
            )

    @staticmethod
    def _session_from_row(row) -> WorkflowSession:
        channel = row["channel"]
        chat_id = row["chat_id"]
        if not isinstance(channel, str) or not isinstance(chat_id, str):
            raise ValueError("workflow identity is incomplete")
        return WorkflowSession(
            id=row["id"],
            kind=row["kind"],
            phase=row["phase"],
            idempotency_key=row["idempotency_key"],
            customer_id=row["customer_id"],
            channel=channel,
            chat_id=chat_id,
            revision=row["revision"],
            state=_json_object(row["state"], "state"),
            error_code=row["error_code"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _action_from_row(row) -> BookingAction:
        result = row["result"]
        return BookingAction(
            id=row["id"],
            scenario_id=row["scenario_id"],
            customer_id=row["customer_id"],
            channel=row["channel"],
            chat_id=row["chat_id"],
            revision=row["revision"],
            action_kind=row["action_kind"],
            payload=_json_object(row["payload"], "payload"),
            expires_at=row["expires_at"],
            consumed_at=row["consumed_at"],
            result=(
                _json_object(result, "result") if result is not None else None
            ),
        )

    @staticmethod
    async def _insert_event(
        connection,
        scenario_id: UUID,
        event_type: str,
        payload: Mapping[str, object] | str,
        created_at: datetime,
        *,
        encoded: bool = False,
    ) -> None:
        payload_json = (
            payload
            if encoded
            else _dump_json_object(payload, "payload")
        )
        await connection.execute(
            """
            INSERT INTO booking_events
                (id, scenario_id, event_type, payload, created_at)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            """,
            uuid4(),
            scenario_id,
            event_type,
            payload_json,
            created_at,
        )
