import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import asyncpg

from moroz.booking.models import BookingEvent, BookingScenario, ExternalBooking
from moroz.common.db import Database
from moroz.notifications.planner import plan_booking_notifications


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _dump_json(value: object) -> str:
    return json.dumps(_thaw_json(value), ensure_ascii=False)


def _load_json(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


class BookingRepository:
    def __init__(self, database: Database):
        self._database = database

    @asynccontextmanager
    async def serialized_scenario(
        self,
        scenario_id: UUID,
    ) -> AsyncIterator["BookingScenarioSession"]:
        async with self._database.acquire() as connection:
            lock_key = f"booking:scenario:{scenario_id}"
            await connection.execute(
                "SELECT pg_advisory_lock(hashtextextended($1, 0))",
                lock_key,
            )
            try:
                row = await connection.fetchrow(
                    "SELECT * FROM booking_scenarios WHERE id = $1",
                    scenario_id,
                )
                if row is None:
                    raise KeyError(f"booking scenario {scenario_id} not found")
                yield BookingScenarioSession(
                    self,
                    connection,
                    self._scenario_from_row(row),
                )
            finally:
                await connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
                    lock_key,
                )

    async def create_scenario(self, scenario: BookingScenario) -> UUID:
        async with self._database.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    INSERT INTO booking_scenarios
                        (id, kind, phase, idempotency_key, customer_id, state,
                         error_code, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id
                    """,
                    scenario.id,
                    scenario.kind,
                    scenario.phase,
                    scenario.idempotency_key,
                    scenario.customer_id,
                    _dump_json(scenario.state),
                    scenario.error_code,
                    scenario.created_at,
                    scenario.updated_at,
                )
                if row is None:
                    return await connection.fetchval(
                        """
                        SELECT id FROM booking_scenarios
                        WHERE idempotency_key = $1
                        """,
                        scenario.idempotency_key,
                    )
                await self._insert_event(
                    connection,
                    scenario.id,
                    "booking_scenario_created",
                    {},
                )
                return row["id"]

    async def get_scenario(self, scenario_id: UUID) -> BookingScenario | None:
        async with self._database.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM booking_scenarios WHERE id = $1",
                scenario_id,
            )
        if row is None:
            return None
        return self._scenario_from_row(row)

    @staticmethod
    def _scenario_from_row(row) -> BookingScenario:
        return BookingScenario(
            id=row["id"],
            kind=row["kind"],
            phase=row["phase"],
            idempotency_key=row["idempotency_key"],
            customer_id=row["customer_id"],
            state=_load_json(row["state"]),
            error_code=row["error_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def checkpoint(
        self,
        scenario: BookingScenario,
        event_type: str,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        async with self._database.acquire() as connection:
            async with connection.transaction():
                await self._lock_scenario(connection, scenario.id)
                await self._checkpoint_with_connection(
                    connection, scenario, event_type, payload
                )

    async def confirm(
        self,
        scenario: BookingScenario,
        booking: ExternalBooking,
    ) -> None:
        await self._complete(scenario, booking, "booking_confirmed")

    async def escalate(
        self,
        scenario: BookingScenario,
        error_code: str,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        async with self._database.acquire() as connection:
            async with connection.transaction():
                await self._lock_scenario(connection, scenario.id)
                await self._escalate_with_connection(
                    connection, scenario, error_code, payload
                )

    async def complete_cancellation(
        self,
        scenario: BookingScenario,
        booking: ExternalBooking,
    ) -> None:
        await self._complete(scenario, booking, "booking_cancelled")

    async def get_local_booking(
        self,
        scenario_id: UUID,
    ) -> ExternalBooking | None:
        async with self._database.acquire() as connection:
            return await self._get_local_booking_with_connection(
                connection, scenario_id
            )

    @staticmethod
    async def _get_local_booking_with_connection(
        connection: asyncpg.Connection,
        scenario_id: UUID,
    ) -> ExternalBooking | None:
        row = await connection.fetchrow(
            """
            SELECT b.external_id, b.customer_id, b.booking_key, b.slot_id,
                   b.starts_at, b.scheduled_end_at, b.status
            FROM booking_scenarios AS s
            JOIN bookings AS b
              ON b.external_id = s.state->>'external_id'
            WHERE s.id = $1
            """,
            scenario_id,
        )
        if row is None:
            return None
        return ExternalBooking(
            external_id=row["external_id"],
            customer_id=row["customer_id"],
            booking_key=row["booking_key"],
            slot_id=row["slot_id"],
            starts_at=row["starts_at"],
            status=row["status"],
            scheduled_end_at=row["scheduled_end_at"],
        )

    @staticmethod
    async def _has_unresolved_outcome_with_connection(
        connection: asyncpg.Connection,
        external_id: str,
    ) -> bool:
        return bool(
            await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM booking_scenarios
                    WHERE state->>'external_id' = $1
                      AND (
                          phase = 'executing'
                          OR (phase = 'escalated'
                              AND error_code = 'booking_outcome_unknown')
                      )
                )
                """,
                external_id,
            )
        )

    async def list_events(self, scenario_id: UUID) -> list[BookingEvent]:
        async with self._database.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT id, scenario_id, event_type, payload, created_at
                FROM booking_events
                WHERE scenario_id = $1
                ORDER BY created_at, id
                """,
                scenario_id,
            )
        return [
            BookingEvent(
                id=row["id"],
                scenario_id=row["scenario_id"],
                event_type=row["event_type"],
                payload=_load_json(row["payload"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def _complete(
        self,
        scenario: BookingScenario,
        booking: ExternalBooking,
        event_type: str,
    ) -> None:
        async with self._database.acquire() as connection:
            async with connection.transaction():
                await self._lock_scenario(connection, scenario.id)
                await self._complete_with_connection(
                    connection, scenario, booking, event_type
                )

    async def _complete_with_connection(
        self,
        connection: asyncpg.Connection,
        scenario: BookingScenario,
        booking: ExternalBooking,
        event_type: str,
    ) -> None:
        state = dict(_thaw_json(scenario.state))
        state["external_id"] = booking.external_id
        snapshot = {
            "external_id": booking.external_id,
            "customer_id": booking.customer_id,
            "booking_key": str(booking.booking_key),
            "slot_id": booking.slot_id,
            "starts_at": booking.starts_at.isoformat(),
            "scheduled_end_at": (
                booking.scheduled_end_at.isoformat()
                if booking.scheduled_end_at is not None
                else None
            ),
            "status": booking.status,
        }
        await self._update_scenario(connection, scenario, state=state)
        stored_external_id = await connection.fetchval(
            """
            INSERT INTO bookings
                (id, last_scenario_id, external_id, customer_id,
                 booking_key, slot_id, starts_at, scheduled_end_at, status, snapshot)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
            ON CONFLICT (external_id) DO UPDATE SET
                last_scenario_id = EXCLUDED.last_scenario_id,
                slot_id = EXCLUDED.slot_id,
                starts_at = EXCLUDED.starts_at,
                scheduled_end_at = EXCLUDED.scheduled_end_at,
                status = EXCLUDED.status,
                snapshot = EXCLUDED.snapshot,
                updated_at = now()
            WHERE bookings.customer_id = EXCLUDED.customer_id
              AND bookings.booking_key = EXCLUDED.booking_key
            RETURNING external_id
            """,
            uuid4(),
            scenario.id,
            booking.external_id,
            booking.customer_id,
            booking.booking_key,
            booking.slot_id,
            booking.starts_at,
            booking.scheduled_end_at,
            booking.status,
            _dump_json(snapshot),
        )
        if stored_external_id is None:
            raise RuntimeError("booking ownership conflict")
        await self._insert_event(
            connection,
            scenario.id,
            event_type,
            {
                "external_id": booking.external_id,
                "status": booking.status,
            },
        )
        await self._sync_notification_jobs(
            connection,
            booking,
            now=scenario.updated_at,
        )

    @staticmethod
    async def _sync_notification_jobs(
        connection: asyncpg.Connection,
        booking: ExternalBooking,
        *,
        now,
    ) -> None:
        await connection.execute(
            """
            UPDATE scheduler_jobs
            SET status = 'skipped',
                finished_at = now(),
                last_error_code = 'stale',
                updated_at = now()
            WHERE booking_key = $1
              AND status IN ('pending', 'claimed')
              AND ($2::timestamptz IS NULL
                   OR booking_starts_at IS DISTINCT FROM $2)
            """,
            booking.booking_key,
            booking.starts_at if booking.status == "confirmed" else None,
        )
        if booking.status != "confirmed":
            return
        jobs = plan_booking_notifications(
            booking_key=booking.booking_key,
            starts_at=booking.starts_at,
            now=now,
        )
        await connection.executemany(
            """
            INSERT INTO scheduler_jobs
                (id, kind, run_at, payload, idempotency_key, status,
                 attempts, booking_key, booking_starts_at,
                 created_at, updated_at)
            VALUES ($1, $2, $3, $4::jsonb, $5, 'pending', 0, $6, $7, $8, $8)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            [
                (
                    uuid4(),
                    job.kind,
                    job.run_at,
                    _dump_json(job.payload),
                    job.idempotency_key,
                    job.booking_key,
                    job.booking_starts_at,
                    now,
                )
                for job in jobs
            ],
        )

    async def _checkpoint_with_connection(
        self,
        connection: asyncpg.Connection,
        scenario: BookingScenario,
        event_type: str,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        await self._update_scenario(connection, scenario)
        await self._insert_event(
            connection,
            scenario.id,
            event_type,
            payload or {},
        )

    async def _escalate_with_connection(
        self,
        connection: asyncpg.Connection,
        scenario: BookingScenario,
        error_code: str,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        event_payload = dict(_thaw_json(payload or {}))
        event_payload["error_code"] = error_code
        await self._update_scenario(
            connection,
            scenario,
            error_code=error_code,
        )
        await self._insert_event(
            connection,
            scenario.id,
            "admin_attention_required",
            event_payload,
        )

    @staticmethod
    async def _lock_scenario(connection, scenario_id: UUID) -> None:
        row = await connection.fetchrow(
            "SELECT id FROM booking_scenarios WHERE id = $1 FOR UPDATE",
            scenario_id,
        )
        if row is None:
            raise KeyError(f"booking scenario {scenario_id} not found")

    @staticmethod
    async def _update_scenario(
        connection,
        scenario: BookingScenario,
        *,
        state: Mapping[str, object] | None = None,
        error_code: str | None = None,
    ) -> None:
        await connection.execute(
            """
            UPDATE booking_scenarios
            SET phase = $2, state = $3::jsonb, error_code = $4,
                updated_at = $5
            WHERE id = $1
            """,
            scenario.id,
            scenario.phase,
            _dump_json(scenario.state if state is None else state),
            scenario.error_code if error_code is None else error_code,
            scenario.updated_at,
        )

    @staticmethod
    async def _insert_event(
        connection,
        scenario_id: UUID,
        event_type: str,
        payload: Mapping[str, object],
    ) -> None:
        await connection.execute(
            """
            INSERT INTO booking_events
                (id, scenario_id, event_type, payload)
            VALUES ($1, $2, $3, $4::jsonb)
            """,
            uuid4(),
            scenario_id,
            event_type,
            _dump_json(payload),
        )


class BookingScenarioSession:
    def __init__(
        self,
        repository: BookingRepository,
        connection: asyncpg.Connection,
        scenario: BookingScenario,
    ) -> None:
        self._repository = repository
        self._connection = connection
        self.scenario = scenario

    @asynccontextmanager
    async def serialized_booking(self, external_id: str) -> AsyncIterator[None]:
        lock_key = f"booking:external:{external_id}"
        await self._connection.execute(
            "SELECT pg_advisory_lock(hashtextextended($1, 0))",
            lock_key,
        )
        try:
            yield
        finally:
            await self._connection.execute(
                "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
                lock_key,
            )

    async def complete_cancellation(
        self,
        scenario: BookingScenario,
        booking: ExternalBooking,
    ) -> None:
        async with self._connection.transaction():
            await self._repository._lock_scenario(self._connection, scenario.id)
            await self._repository._complete_with_connection(
                self._connection, scenario, booking, "booking_cancelled"
            )
        self.scenario = scenario

    async def checkpoint(
        self,
        scenario: BookingScenario,
        event_type: str,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        async with self._connection.transaction():
            await self._repository._lock_scenario(self._connection, scenario.id)
            await self._repository._checkpoint_with_connection(
                self._connection, scenario, event_type, payload
            )
        self.scenario = scenario

    async def confirm(
        self,
        scenario: BookingScenario,
        booking: ExternalBooking,
    ) -> None:
        async with self._connection.transaction():
            await self._repository._lock_scenario(self._connection, scenario.id)
            await self._repository._complete_with_connection(
                self._connection, scenario, booking, "booking_confirmed"
            )
        self.scenario = scenario

    async def escalate(
        self,
        scenario: BookingScenario,
        error_code: str,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        async with self._connection.transaction():
            await self._repository._lock_scenario(self._connection, scenario.id)
            await self._repository._escalate_with_connection(
                self._connection, scenario, error_code, payload
            )
        self.scenario = scenario

    async def get_local_booking(self) -> ExternalBooking | None:
        return await self._repository._get_local_booking_with_connection(
            self._connection,
            self.scenario.id,
        )

    async def has_unresolved_outcome(self, external_id: str) -> bool:
        return await self._repository._has_unresolved_outcome_with_connection(
            self._connection,
            external_id,
        )
