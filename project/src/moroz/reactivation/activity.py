from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from types import MappingProxyType
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from moroz.booking.yclients_http import (
    YclientsConfig,
    YclientsHttpClient,
    YclientsTransportError,
)
from moroz.booking.yclients_records import (
    ProjectionRecord,
    YclientsProjectionError,
    _page_data,
    _projection_record,
)
from moroz.common.db import Database
from moroz.notifications.models import JobResult, PlannedSchedulerJob
from moroz.notifications.repository import SchedulerJobRepository


MAX_HISTORY_PAGES = 20
ACTIVITY_SYNC_BATCH = 25
ACTIVITY_SYNC_INTERVAL = timedelta(minutes=10)
HISTORY_FRESHNESS = timedelta(hours=24)
RECENT_BOOKINGS_FRESHNESS = timedelta(minutes=15)
ACTIVITY_SOURCE_VERSION = "yclients-client-history-v1"
_PAGE_SIZE = 100
ACTIVITY_SYNC_KIND = "yclients_activity_sync"
ACTIVITY_LOCK = "yclients_activity_sync:v1"
_SAFE_PROVIDER_ERROR_CODES = frozenset(
    {
        "yclients_transport",
        "yclients_http_status",
        "yclients_response_shape",
        "yclients_page_bound",
        "history_page_limit",
        "yclients_identity_missing",
        "yclients_provider_error",
    }
)


@dataclass(frozen=True, slots=True)
class ClientActivitySnapshot:
    yclients_client_id: str
    last_completed_visit_at: datetime | None
    next_active_booking_at: datetime | None
    history_synced_at: datetime
    source_version: str
    sync_status: Literal["current", "partial", "error"]
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ActivityCandidate:
    channel: str
    user_id: str
    identity_status: Literal["unverified", "verified", "conflict"]
    yclients_client_id: str | None


@dataclass(frozen=True, slots=True)
class LocalBookingProof:
    external_id: str
    booking_key: UUID


@dataclass(frozen=True, slots=True)
class ResolvedIdentity:
    status: Literal["unverified", "verified", "conflict"]
    yclients_client_id: str | None


class YclientsClientHistoryReader:
    def __init__(
        self,
        config: YclientsConfig,
        *,
        http: YclientsHttpClient | None = None,
    ) -> None:
        self._config = config
        self._http = http or YclientsHttpClient(config)
        self._timezone = ZoneInfo(config.timezone_name)

    async def read_history(
        self,
        yclients_client_id: str,
        *,
        now: datetime,
    ) -> ClientActivitySnapshot:
        client_id = _provider_id(yclients_client_id)
        current = _aware_utc(now)
        records: list[ProjectionRecord] = []
        external_ids: set[str] = set()
        for page in range(1, MAX_HISTORY_PAGES + 1):
            data = await self._read_page(client_id, page)
            try:
                parsed = tuple(
                    _projection_record(item, self._timezone) for item in data
                )
            except (TypeError, ValueError, OverflowError) as error:
                raise YclientsProjectionError("yclients_response_shape") from error
            if any(record.client_id != client_id for record in parsed):
                raise YclientsProjectionError("yclients_response_shape")
            page_ids = {record.external_id for record in parsed}
            if len(page_ids) != len(parsed) or page_ids & external_ids:
                raise YclientsProjectionError("yclients_response_shape")
            external_ids.update(page_ids)
            records.extend(parsed)
            if len(data) < _PAGE_SIZE:
                return _activity_snapshot(client_id, records, current, "current", None)
        return _activity_snapshot(
            client_id,
            records,
            current,
            "partial",
            "history_page_limit",
        )

    async def read_record(self, external_id: str) -> ProjectionRecord | None:
        provider_id = _provider_id(external_id)
        try:
            response = await self._http.request(
                "GET",
                f"/api/v1/record/{self._config.company_id}/{provider_id}",
                user_auth=True,
            )
        except YclientsTransportError as error:
            raise YclientsProjectionError("yclients_transport") from error
        if response.status == 404:
            return None
        if response.status != 200:
            raise YclientsProjectionError("yclients_http_status")
        try:
            envelope = json.loads(response.body)
            if not isinstance(envelope, dict) or envelope.get("success") is not True:
                raise ValueError("record envelope is malformed")
            data = envelope.get("data")
            if isinstance(data, list):
                if len(data) != 1:
                    raise ValueError("record envelope is malformed")
                data = data[0]
            if not isinstance(data, dict):
                raise ValueError("record envelope is malformed")
            record = _projection_record(data, self._timezone)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError, OverflowError) as error:
            raise YclientsProjectionError("yclients_response_shape") from error
        if record.external_id != provider_id:
            raise YclientsProjectionError("yclients_response_shape")
        return record

    async def _read_page(self, client_id: str, page: int) -> list[object]:
        try:
            response = await self._http.request(
                "GET",
                f"/api/v1/records/{self._config.company_id}",
                query=(
                    ("client_id", client_id),
                    ("page", page),
                    ("count", _PAGE_SIZE),
                    ("with_deleted", 1),
                ),
                user_auth=True,
            )
        except YclientsTransportError as error:
            raise YclientsProjectionError("yclients_transport") from error
        if response.status != 200:
            raise YclientsProjectionError("yclients_http_status")
        data = _page_data(response)
        if len(data) > _PAGE_SIZE:
            raise YclientsProjectionError("yclients_response_shape")
        return data


class ActivityRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    @asynccontextmanager
    async def serialized(self):
        async with self._database.acquire() as connection:
            try:
                locked = await connection.fetchval(
                    "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
                    ACTIVITY_LOCK,
                )
            except asyncpg.PostgresError as error:
                raise YclientsProjectionError("yclients_activity_write") from error
            try:
                yield connection if locked else None
            finally:
                if locked:
                    try:
                        await connection.execute(
                            "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
                            ACTIVITY_LOCK,
                        )
                    except asyncpg.PostgresError as error:
                        raise YclientsProjectionError("yclients_activity_write") from error

    async def prepare_candidates(self, connection) -> None:
        await connection.execute(
            """
            INSERT INTO customer_activity_projection
                (channel, user_id, identity_status, sync_status)
            SELECT consent.channel, consent.user_id, 'unverified', 'never'
            FROM marketing_consents AS consent
            WHERE consent.channel = 'telegram'
            ON CONFLICT (channel, user_id) DO NOTHING
            """
        )

    async def claim_candidates(
        self,
        connection,
        *,
        now: datetime,
        limit: int = ACTIVITY_SYNC_BATCH,
    ) -> tuple[ActivityCandidate, ...]:
        if limit <= 0:
            return ()
        cutoff = _aware_utc(now) - HISTORY_FRESHNESS + ACTIVITY_SYNC_INTERVAL
        rows = await connection.fetch(
            """
            SELECT channel, user_id, identity_status, yclients_client_id
            FROM customer_activity_projection
            WHERE identity_status = 'unverified'
               OR (identity_status = 'verified'
                   AND (history_synced_at IS NULL OR history_synced_at <= $1))
            ORDER BY (identity_status = 'verified') DESC,
                     CASE WHEN identity_status = 'verified'
                          THEN history_synced_at ELSE updated_at END NULLS FIRST,
                     channel, user_id
            FOR UPDATE SKIP LOCKED
            LIMIT $2
            """,
            cutoff,
            min(limit, ACTIVITY_SYNC_BATCH),
        )
        return tuple(
            ActivityCandidate(
                row["channel"],
                row["user_id"],
                row["identity_status"],
                row["yclients_client_id"],
            )
            for row in rows
        )

    async def current_identity_client_ids(
        self,
        connection,
        candidate: ActivityCandidate,
    ) -> tuple[str, ...]:
        if candidate.channel != "telegram":
            return ()
        rows = await connection.fetch(
            """
            SELECT DISTINCT projection.client_id
            FROM yclients_booking_projection AS projection
            JOIN bookings AS booking
              ON booking.external_id = projection.external_id
             AND booking.booking_key = projection.booking_key
            WHERE booking.customer_id = $1
              AND projection.bot_marker_state = 'valid'
              AND projection.client_id IS NOT NULL
            ORDER BY projection.client_id
            """,
            candidate.user_id,
        )
        return tuple(row["client_id"] for row in rows)

    async def latest_local_booking(
        self,
        connection,
        candidate: ActivityCandidate,
    ) -> LocalBookingProof | None:
        if candidate.channel != "telegram":
            return None
        row = await connection.fetchrow(
            """
            SELECT external_id, booking_key
            FROM bookings
            WHERE customer_id = $1
            ORDER BY starts_at DESC, updated_at DESC, external_id DESC
            LIMIT 1
            """,
            candidate.user_id,
        )
        return (
            LocalBookingProof(row["external_id"], row["booking_key"])
            if row is not None
            else None
        )

    async def resolve_identity(
        self,
        connection,
        candidate: ActivityCandidate,
        client_ids,
        *,
        now: datetime,
    ) -> ResolvedIdentity:
        proposed = tuple(sorted({_provider_id(value) for value in client_ids}))
        verified_at = _aware_utc(now)
        async with connection.transaction():
            current = await connection.fetchrow(
                """
                SELECT channel, user_id, yclients_client_id, identity_status
                FROM customer_activity_projection
                WHERE channel = $1 AND user_id = $2
                FOR UPDATE
                """,
                candidate.channel,
                candidate.user_id,
            )
            if current is None:
                return ResolvedIdentity("unverified", None)
            if current["identity_status"] == "conflict":
                return ResolvedIdentity("conflict", current["yclients_client_id"])
            if not proposed:
                await connection.execute(
                    """
                    UPDATE customer_activity_projection
                    SET updated_at = $3
                    WHERE channel = $1 AND user_id = $2
                    """,
                    candidate.channel,
                    candidate.user_id,
                    verified_at,
                )
                return ResolvedIdentity(
                    current["identity_status"], current["yclients_client_id"]
                )
            affected_ids = set(proposed)
            if current["yclients_client_id"] is not None:
                affected_ids.add(current["yclients_client_id"])
            related = await connection.fetch(
                """
                SELECT channel, user_id, yclients_client_id, identity_status
                FROM customer_activity_projection
                WHERE yclients_client_id = ANY($1::text[])
                FOR UPDATE
                """,
                sorted(affected_ids),
            )
            target = proposed[0] if len(proposed) == 1 else None
            changed = (
                current["identity_status"] == "verified"
                and current["yclients_client_id"] != target
            )
            claimed_elsewhere = any(
                (row["channel"], row["user_id"])
                != (candidate.channel, candidate.user_id)
                for row in related
                if row["yclients_client_id"] in proposed
            )
            if target is None or changed or claimed_elsewhere:
                await connection.execute(
                    """
                    UPDATE customer_activity_projection
                    SET identity_status = 'conflict', updated_at = $4
                    WHERE (channel = $1 AND user_id = $2)
                       OR yclients_client_id = ANY($3::text[])
                    """,
                    candidate.channel,
                    candidate.user_id,
                    sorted(affected_ids),
                    verified_at,
                )
                return ResolvedIdentity("conflict", current["yclients_client_id"])
            await connection.execute(
                """
                UPDATE customer_activity_projection
                SET yclients_client_id = $3,
                    identity_status = 'verified',
                    identity_source = 'moroz_booking_key',
                    identity_verified_at = $4,
                    updated_at = $4
                WHERE channel = $1 AND user_id = $2
                """,
                candidate.channel,
                candidate.user_id,
                target,
                verified_at,
            )
        return ResolvedIdentity("verified", target)

    async def apply_snapshot(
        self,
        connection,
        candidate: ActivityCandidate,
        snapshot: ClientActivitySnapshot,
    ) -> None:
        if snapshot.sync_status == "current":
            await connection.execute(
                """
                UPDATE customer_activity_projection
                SET last_completed_visit_at = $4,
                    history_synced_at = $5,
                    source_version = $6,
                    sync_status = 'current',
                    sync_error_code = NULL,
                    updated_at = $5
                WHERE channel = $1 AND user_id = $2
                  AND identity_status = 'verified'
                  AND yclients_client_id = $3
                """,
                candidate.channel,
                candidate.user_id,
                snapshot.yclients_client_id,
                snapshot.last_completed_visit_at,
                snapshot.history_synced_at,
                snapshot.source_version,
            )
            return
        code = (
            "history_page_limit"
            if snapshot.sync_status == "partial"
            and snapshot.error_code == "history_page_limit"
            else _safe_error_code(snapshot.error_code or "")
        )
        await connection.execute(
            """
            UPDATE customer_activity_projection
            SET sync_status = $4, sync_error_code = $5, updated_at = $6
            WHERE channel = $1 AND user_id = $2
              AND identity_status = 'verified'
              AND yclients_client_id = $3
            """,
            candidate.channel,
            candidate.user_id,
            snapshot.yclients_client_id,
            snapshot.sync_status,
            code,
            snapshot.history_synced_at,
        )

    async def record_error(
        self,
        connection,
        candidate: ActivityCandidate,
        error_code: str,
        *,
        now: datetime,
    ) -> None:
        await connection.execute(
            """
            UPDATE customer_activity_projection
            SET sync_status = 'error', sync_error_code = $3, updated_at = $4
            WHERE channel = $1 AND user_id = $2
              AND identity_status != 'conflict'
            """,
            candidate.channel,
            candidate.user_id,
            _safe_error_code(error_code),
            _aware_utc(now),
        )


def activity_job(now: datetime) -> PlannedSchedulerJob:
    utc = _aware_utc(now)
    bucket = utc.replace(minute=(utc.minute // 10) * 10, second=0, microsecond=0)
    return PlannedSchedulerJob(
        kind=ACTIVITY_SYNC_KIND,
        run_at=bucket,
        payload=MappingProxyType({}),
        idempotency_key=f"{ACTIVITY_SYNC_KIND}:{bucket.isoformat()}",
        booking_key=None,
        booking_starts_at=None,
    )


class ActivitySyncCoordinator:
    def __init__(
        self,
        repository,
        reader: YclientsClientHistoryReader,
        scheduler: SchedulerJobRepository,
        *,
        clock,
    ) -> None:
        self._repository = repository
        self._reader = reader
        self._scheduler = scheduler
        self._clock = clock

    async def ensure_current(self, now: datetime) -> None:
        await self._scheduler.schedule(activity_job(now))

    async def run(self, job: PlannedSchedulerJob) -> JobResult:
        await self._scheduler.schedule(activity_job(job.run_at + ACTIVITY_SYNC_INTERVAL))
        now = _aware_utc(self._clock())
        async with self._repository.serialized() as connection:
            if connection is None:
                return JobResult.skipped("activity_busy")
            await self._repository.prepare_candidates(connection)
            candidates = await self._repository.claim_candidates(
                connection,
                now=now,
                limit=ACTIVITY_SYNC_BATCH,
            )
            for candidate in candidates:
                await self._sync_candidate(connection, candidate, now)
        return JobResult.sent()

    async def _sync_candidate(self, connection, candidate: ActivityCandidate, now: datetime) -> None:
        if candidate.identity_status == "conflict":
            return
        client_id = candidate.yclients_client_id
        if candidate.identity_status == "unverified":
            try:
                client_ids = await self._identity_client_ids(connection, candidate)
            except ValueError:
                await self._repository.record_error(
                    connection,
                    candidate,
                    "yclients_identity_missing",
                    now=now,
                )
                return
            except YclientsProjectionError as error:
                await self._repository.record_error(
                    connection,
                    candidate,
                    _safe_error_code(error.code),
                    now=now,
                )
                return
            resolved = await self._repository.resolve_identity(
                connection,
                candidate,
                client_ids,
                now=now,
            )
            if resolved.status != "verified":
                return
            client_id = resolved.yclients_client_id
        else:
            current_ids = await self._repository.current_identity_client_ids(
                connection,
                candidate,
            )
            if current_ids:
                resolved = await self._repository.resolve_identity(
                    connection,
                    candidate,
                    current_ids,
                    now=now,
                )
                if resolved.status != "verified":
                    return
                client_id = resolved.yclients_client_id
        if client_id is None:
            await self._repository.record_error(
                connection,
                candidate,
                "yclients_identity_missing",
                now=now,
            )
            return
        try:
            snapshot = await self._reader.read_history(client_id, now=now)
        except ValueError:
            await self._repository.record_error(
                connection,
                candidate,
                "yclients_identity_missing",
                now=now,
            )
            return
        except YclientsProjectionError as error:
            await self._repository.record_error(
                connection,
                candidate,
                _safe_error_code(error.code),
                now=now,
            )
            return
        await self._repository.apply_snapshot(connection, candidate, snapshot)

    async def _identity_client_ids(
        self,
        connection,
        candidate: ActivityCandidate,
    ) -> tuple[str, ...]:
        current = await self._repository.current_identity_client_ids(
            connection,
            candidate,
        )
        if current:
            return tuple(current)
        local = await self._repository.latest_local_booking(connection, candidate)
        if local is None:
            return ()
        record = await self._reader.read_record(local.external_id)
        if (
            record is None
            or record.external_id != local.external_id
            or record.bot_marker_state != "valid"
            or record.booking_key != local.booking_key
            or record.client_id is None
        ):
            return ()
        return (record.client_id,)


def _activity_snapshot(
    client_id: str,
    records: list[ProjectionRecord],
    now: datetime,
    status: Literal["current", "partial"],
    error_code: str | None,
) -> ClientActivitySnapshot:
    completed = [
        record.starts_at.astimezone(UTC)
        for record in records
        if record.status == "completed" and not record.deleted and record.starts_at <= now
    ]
    future = [
        record.starts_at.astimezone(UTC)
        for record in records
        if record.status == "confirmed" and not record.deleted and record.starts_at >= now
    ]
    return ClientActivitySnapshot(
        yclients_client_id=client_id,
        last_completed_visit_at=max(completed, default=None),
        next_active_booking_at=min(future, default=None),
        history_synced_at=now,
        source_version=ACTIVITY_SOURCE_VERSION,
        sync_status=status,
        error_code=error_code,
    )


def _provider_id(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("provider id is malformed")
    if not value.isascii() or not value.isdigit() or value.startswith("0"):
        raise ValueError("provider id is malformed")
    return value


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)


def _safe_error_code(code: str) -> str:
    return code if code in _SAFE_PROVIDER_ERROR_CODES else "yclients_provider_error"
