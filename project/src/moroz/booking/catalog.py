from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import re
from types import MappingProxyType
from typing import Literal

import asyncpg

from moroz.booking.yclients_catalog import (
    CatalogRecord,
    CatalogSnapshot,
    YclientsCatalogError,
)
from moroz.common.db import Database
from moroz.notifications.models import JobResult, PlannedSchedulerJob
from moroz.notifications.repository import SchedulerJobRepository


CATALOG_LOCK = "yclients_service_catalog:v1"
CATALOG_SYNC_KIND = "yclients_service_catalog_sync"
CATALOG_MAX_AGE = timedelta(hours=24)
_MAX_MATCHES = 5
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_IGNORED_MATCH_TOKENS = frozenset(
    {"услуга", "услуги", "услугу", "процедура", "процедуры", "процедуру"}
)
_PRICE_WORDS = frozenset(
    {"цена", "цену", "цене", "ценой", "цены", "стоит", "стоимость", "прайс"}
)
_DURATION_WORDS = frozenset(
    {"длительность", "длится", "времени", "минут", "минута", "минуты"}
)
_STAFF_WORDS = frozenset(
    {"мастер", "мастера", "специалист", "специалисты", "кто", "сотрудник"}
)
_COMPARISON_WORDS = frozenset(
    {"сравни", "сравнить", "разница", "отличается", "лучше", "подобрать", "выбрать"}
)


@dataclass(frozen=True, slots=True)
class CatalogVariant:
    staff_id: str
    staff_name: str
    price_min: Decimal
    price_max: Decimal
    duration_minutes: int


@dataclass(frozen=True, slots=True)
class CatalogService:
    service_id: str
    service_name: str
    category_name: str | None
    variants: tuple[CatalogVariant, ...]


@dataclass(frozen=True, slots=True)
class CatalogGrounding:
    status: Literal["fresh", "stale", "missing"]
    services: tuple[CatalogService, ...]
    simple_kind: Literal["price", "duration", "staff"] | None
    ambiguous: bool


class CatalogRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    @asynccontextmanager
    async def serialized(self):
        async with self._database.acquire() as connection:
            try:
                locked = await connection.fetchval(
                    "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
                    CATALOG_LOCK,
                )
            except asyncpg.PostgresError as error:
                raise YclientsCatalogError("yclients_catalog_write") from error
            try:
                yield connection if locked else None
            finally:
                if locked:
                    try:
                        await connection.execute(
                            "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
                            CATALOG_LOCK,
                        )
                    except asyncpg.PostgresError as error:
                        raise YclientsCatalogError(
                            "yclients_catalog_write"
                        ) from error

    async def replace(self, connection, snapshot: CatalogSnapshot) -> None:
        rows = [
            (
                record.service_id,
                record.staff_id,
                record.service_name,
                record.category_name,
                record.staff_name,
                record.price_min,
                record.price_max,
                record.duration_minutes,
                snapshot.synced_at,
            )
            for record in snapshot.records
        ]
        try:
            async with connection.transaction():
                await connection.execute("DELETE FROM yclients_service_catalog")
                if rows:
                    await connection.executemany(
                        """
                        INSERT INTO yclients_service_catalog
                            (service_id, staff_id, service_name, category_name,
                             staff_name, price_min, price_max,
                             duration_minutes, synced_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                        rows,
                    )
        except asyncpg.PostgresError as error:
            raise YclientsCatalogError("yclients_catalog_write") from error

    async def ground(
        self,
        connection,
        text: str,
        now: datetime,
    ) -> CatalogGrounding:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        freshness = await connection.fetchrow(
            """
            SELECT
                (SELECT MAX(synced_at) FROM yclients_service_catalog)
                    AS projection_synced_at,
                (SELECT MAX(finished_at) FROM scheduler_jobs
                 WHERE kind = $1 AND status = $2 AND finished_at IS NOT NULL)
                    AS empty_snapshot_at
            """,
            CATALOG_SYNC_KIND,
            "finished",
        )
        last_success = (
            freshness["projection_synced_at"] or freshness["empty_snapshot_at"]
        )
        kind = _simple_kind(_tokens(text))
        if last_success is None:
            return CatalogGrounding("missing", (), kind, False)
        if now - last_success > CATALOG_MAX_AGE:
            return CatalogGrounding("stale", (), kind, False)
        rows = await connection.fetch(
            """
            SELECT service_id, staff_id, service_name, category_name,
                   staff_name, price_min, price_max, duration_minutes
            FROM yclients_service_catalog
            ORDER BY service_id, staff_id
            """
        )
        records = tuple(
            _record_from_row(row)
            for row in rows
        )
        return match_catalog(records, text)


def catalog_job(now: datetime) -> PlannedSchedulerJob:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    bucket = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return PlannedSchedulerJob(
        kind=CATALOG_SYNC_KIND,
        run_at=bucket,
        payload=MappingProxyType({}),
        idempotency_key=f"{CATALOG_SYNC_KIND}:{bucket.isoformat()}",
        booking_key=None,
        booking_starts_at=None,
    )


class CatalogSyncCoordinator:
    def __init__(
        self,
        repository: CatalogRepository,
        reader,
        scheduler: SchedulerJobRepository,
        *,
        clock,
    ) -> None:
        self._repository = repository
        self._reader = reader
        self._scheduler = scheduler
        self._clock = clock

    async def ensure_current(self, now: datetime) -> None:
        await self._scheduler.schedule(catalog_job(now))

    async def run(self, job: PlannedSchedulerJob) -> JobResult:
        await self._scheduler.schedule(catalog_job(job.run_at + timedelta(hours=1)))
        async with self._repository.serialized() as connection:
            if connection is None:
                return JobResult.skipped("catalog_busy")
            snapshot = await self._reader.read(self._clock())
            await self._repository.replace(connection, snapshot)
        return JobResult.sent()


def match_catalog(records, text: str) -> CatalogGrounding:
    query_tokens = _tokens(text)
    kind = _simple_kind(query_tokens)
    grouped = _group_records(records)
    normalized_query = _normalized_text(text)
    scored: list[tuple[tuple[int, int, int], CatalogService]] = []
    for service in grouped:
        name_tokens = _tokens(service.service_name)
        normalized_name = _normalized_text(service.service_name)
        overlap = len(query_tokens & name_tokens)
        phrase = int(bool(normalized_name and normalized_name in normalized_query))
        if not phrase and not overlap:
            continue
        scored.append(((phrase, overlap, len(name_tokens)), service))
    scored.sort(
        key=lambda item: (
            -item[0][0],
            -item[0][1],
            -item[0][2],
            _normalize(item[1].service_name),
            int(item[1].service_id),
        )
    )
    if not scored:
        return CatalogGrounding("fresh", (), kind, False)
    if kind is None:
        return CatalogGrounding(
            "fresh",
            tuple(service for _, service in scored[:_MAX_MATCHES]),
            None,
            False,
        )
    top_score = scored[0][0]
    top = tuple(
        service for score, service in scored if score == top_score
    )[:_MAX_MATCHES]
    return CatalogGrounding("fresh", top, kind, len(top) > 1)


def _group_records(records) -> tuple[CatalogService, ...]:
    grouped: dict[str, tuple[str, str | None, list[CatalogVariant]]] = {}
    for record in records:
        current = grouped.setdefault(
            record.service_id,
            (record.service_name, record.category_name, []),
        )
        current[2].append(
            CatalogVariant(
                record.staff_id,
                record.staff_name,
                record.price_min,
                record.price_max,
                record.duration_minutes,
            )
        )
    services = [
        CatalogService(
            service_id,
            name,
            category,
            tuple(
                sorted(
                    variants,
                    key=lambda item: (_normalize(item.staff_name), int(item.staff_id)),
                )
            ),
        )
        for service_id, (name, category, variants) in grouped.items()
    ]
    return tuple(
        sorted(
            services,
            key=lambda item: (_normalize(item.service_name), int(item.service_id)),
        )
    )


def _record_from_row(row):
    return CatalogRecord(
        service_id=row["service_id"],
        staff_id=row["staff_id"],
        service_name=row["service_name"],
        category_name=row["category_name"],
        staff_name=row["staff_name"],
        price_min=row["price_min"],
        price_max=row["price_max"],
        duration_minutes=row["duration_minutes"],
    )


def _simple_kind(tokens: frozenset[str]):
    if tokens & _COMPARISON_WORDS:
        return None
    if tokens & _PRICE_WORDS:
        return "price"
    if tokens & _DURATION_WORDS:
        return "duration"
    if tokens & _STAFF_WORDS:
        return "staff"
    return None


def _tokens(text: str) -> frozenset[str]:
    values = frozenset(
        token
        for token in _TOKEN_RE.findall(_normalize(text))
        if len(token) >= 3
    )
    return values - _IGNORED_MATCH_TOKENS


def _normalize(text: str) -> str:
    return text.casefold().replace("ё", "е")


def _normalized_text(text: str) -> str:
    return " ".join(
        token for token in _TOKEN_RE.findall(_normalize(text)) if len(token) >= 3
    )
