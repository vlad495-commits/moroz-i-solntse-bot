import json
from collections.abc import Mapping
from uuid import UUID, uuid4

from moroz.booking.models import BookingEvent, BookingScenario, ExternalBooking
from moroz.common.db import Database


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
                await self._update_scenario(connection, scenario)
                await self._insert_event(
                    connection,
                    scenario.id,
                    event_type,
                    payload or {},
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
        event_payload = dict(_thaw_json(payload or {}))
        event_payload["error_code"] = error_code
        async with self._database.acquire() as connection:
            async with connection.transaction():
                await self._lock_scenario(connection, scenario.id)
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
            row = await connection.fetchrow(
                """
                SELECT b.external_id, b.customer_id, b.slot_id,
                       b.starts_at, b.status
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
            slot_id=row["slot_id"],
            starts_at=row["starts_at"],
            status=row["status"],
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
        state = dict(_thaw_json(scenario.state))
        state["external_id"] = booking.external_id
        snapshot = {
            "external_id": booking.external_id,
            "customer_id": booking.customer_id,
            "slot_id": booking.slot_id,
            "starts_at": booking.starts_at.isoformat(),
            "status": booking.status,
        }
        async with self._database.acquire() as connection:
            async with connection.transaction():
                await self._lock_scenario(connection, scenario.id)
                await self._update_scenario(connection, scenario, state=state)
                await connection.execute(
                    """
                    INSERT INTO bookings
                        (id, last_scenario_id, external_id, customer_id,
                         slot_id, starts_at, status, snapshot)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                    ON CONFLICT (external_id) DO UPDATE SET
                        last_scenario_id = EXCLUDED.last_scenario_id,
                        customer_id = EXCLUDED.customer_id,
                        slot_id = EXCLUDED.slot_id,
                        starts_at = EXCLUDED.starts_at,
                        status = EXCLUDED.status,
                        snapshot = EXCLUDED.snapshot,
                        updated_at = now()
                    """,
                    uuid4(),
                    scenario.id,
                    booking.external_id,
                    booking.customer_id,
                    booking.slot_id,
                    booking.starts_at,
                    booking.status,
                    _dump_json(snapshot),
                )
                await self._insert_event(
                    connection,
                    scenario.id,
                    event_type,
                    {
                        "external_id": booking.external_id,
                        "status": booking.status,
                    },
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
