from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from moroz.booking.yclients_records import ProjectionRecord, YclientsProjectionError
from moroz.notifications.models import JobResult
from moroz.reactivation.activity import (
    ACTIVITY_SOURCE_VERSION,
    ACTIVITY_SYNC_BATCH,
    ACTIVITY_SYNC_INTERVAL,
    ActivityCandidate,
    ActivitySyncCoordinator,
    ClientActivitySnapshot,
    LocalBookingProof,
    ResolvedIdentity,
    activity_job,
)


NOW = datetime(2026, 8, 31, 12, 7, tzinfo=UTC)
BOOKING_KEY = UUID("3b53e155-7fd7-4dd0-9ff3-871e0db59577")


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs = []

    async def schedule(self, job):
        self.jobs.append(job)
        return True


class FakeRepository:
    def __init__(self, candidates, *, current_ids=(), local=None, resolved=None, busy=False):
        self.candidates = list(candidates)
        self.current_ids = tuple(current_ids)
        self.local = local
        self.resolved = resolved
        self.busy = busy
        self.prepared = 0
        self.claims = []
        self.resolutions = []
        self.snapshots = []
        self.errors = []

    @asynccontextmanager
    async def serialized(self):
        yield None if self.busy else object()

    async def prepare_candidates(self, connection):
        self.prepared += 1

    async def claim_candidates(self, connection, *, now, limit):
        self.claims.append((now, limit))
        return self.candidates[:limit]

    async def current_identity_client_ids(self, connection, candidate):
        return self.current_ids

    async def latest_local_booking(self, connection, candidate):
        return self.local

    async def resolve_identity(self, connection, candidate, client_ids, *, now):
        self.resolutions.append((candidate, tuple(client_ids), now))
        return self.resolved or ResolvedIdentity("unverified", None)

    async def apply_snapshot(self, connection, candidate, snapshot):
        self.snapshots.append((candidate, snapshot))

    async def record_error(self, connection, candidate, error_code, *, now):
        self.errors.append((candidate, error_code, now))


class FakeReader:
    def __init__(self, *, record=None, history=None):
        self.record = record
        self.history = history
        self.record_reads = []
        self.history_reads = []

    async def read_record(self, external_id):
        self.record_reads.append(external_id)
        if isinstance(self.record, Exception):
            raise self.record
        return self.record

    async def read_history(self, client_id, *, now):
        self.history_reads.append((client_id, now))
        if isinstance(self.history, Exception):
            raise self.history
        return self.history


def candidate(*, status="unverified", client_id=None):
    return ActivityCandidate("telegram", "42", status, client_id)


def record(*, client_id="55", booking_key=BOOKING_KEY):
    return ProjectionRecord(
        external_id="9001",
        booking_key=booking_key,
        bot_marker_state="valid",
        starts_at=NOW,
        scheduled_end_at=None,
        status="confirmed",
        deleted=False,
        client_name="ignored",
        staff_name=None,
        service_names=(),
        client_id=client_id,
        record_created_at=NOW - timedelta(days=30),
    )


def snapshot(*, status="current", error_code=None):
    return ClientActivitySnapshot(
        yclients_client_id="55",
        last_completed_visit_at=NOW - timedelta(days=100),
        next_active_booking_at=NOW + timedelta(days=1),
        history_synced_at=NOW,
        source_version=ACTIVITY_SOURCE_VERSION,
        sync_status=status,
        error_code=error_code,
    )


@pytest.mark.asyncio
async def test_current_projection_proof_resolves_identity_and_syncs_bounded_batch():
    item = candidate()
    repository = FakeRepository(
        [item],
        current_ids=("55",),
        resolved=ResolvedIdentity("verified", "55"),
    )
    reader = FakeReader(history=snapshot())
    scheduler = FakeScheduler()
    coordinator = ActivitySyncCoordinator(repository, reader, scheduler, clock=lambda: NOW)

    result = await coordinator.run(activity_job(NOW))

    assert result == JobResult.sent()
    assert repository.prepared == 1
    assert repository.claims == [(NOW, ACTIVITY_SYNC_BATCH)]
    assert repository.resolutions == [(item, ("55",), NOW)]
    assert reader.record_reads == []
    assert reader.history_reads == [("55", NOW)]
    assert repository.snapshots == [(item, snapshot())]
    assert scheduler.jobs == [activity_job(NOW + ACTIVITY_SYNC_INTERVAL)]


@pytest.mark.asyncio
async def test_old_local_booking_requires_exact_owner_marker_before_identity_sync():
    item = candidate()
    local = LocalBookingProof("9001", BOOKING_KEY)
    repository = FakeRepository(
        [item],
        local=local,
        resolved=ResolvedIdentity("verified", "55"),
    )
    reader = FakeReader(record=record(), history=snapshot())
    coordinator = ActivitySyncCoordinator(
        repository, reader, FakeScheduler(), clock=lambda: NOW
    )

    await coordinator.run(activity_job(NOW))

    assert reader.record_reads == ["9001"]
    assert repository.resolutions == [(item, ("55",), NOW)]
    assert reader.history_reads == [("55", NOW)]


@pytest.mark.asyncio
async def test_phone_or_name_without_exact_booking_key_never_proves_identity():
    item = candidate()
    repository = FakeRepository(
        [item],
        local=LocalBookingProof("9001", BOOKING_KEY),
    )
    reader = FakeReader(record=record(booking_key=UUID(int=2)))
    coordinator = ActivitySyncCoordinator(
        repository, reader, FakeScheduler(), clock=lambda: NOW
    )

    await coordinator.run(activity_job(NOW))

    assert repository.resolutions == [(item, (), NOW)]
    assert reader.history_reads == []
    assert repository.snapshots == []


@pytest.mark.asyncio
async def test_verified_history_error_is_allowlisted_and_does_not_apply_snapshot():
    item = candidate(status="verified", client_id="55")
    repository = FakeRepository(
        [item],
        current_ids=("55",),
        resolved=ResolvedIdentity("verified", "55"),
    )
    reader = FakeReader(history=YclientsProjectionError("yclients_transport"))
    coordinator = ActivitySyncCoordinator(
        repository, reader, FakeScheduler(), clock=lambda: NOW
    )

    await coordinator.run(activity_job(NOW))

    assert repository.snapshots == []
    assert repository.errors == [(item, "yclients_transport", NOW)]


@pytest.mark.asyncio
async def test_verified_identity_reuses_latest_owned_booking_fallback_and_conflicts():
    item = candidate(status="verified", client_id="55")
    repository = FakeRepository(
        [item],
        local=LocalBookingProof("9001", BOOKING_KEY),
        resolved=ResolvedIdentity("conflict", "55"),
    )
    reader = FakeReader(record=record(client_id="66"), history=snapshot())
    coordinator = ActivitySyncCoordinator(
        repository, reader, FakeScheduler(), clock=lambda: NOW
    )

    await coordinator.run(activity_job(NOW))

    assert reader.record_reads == ["9001"]
    assert repository.resolutions == [(item, ("66",), NOW)]
    assert reader.history_reads == []
    assert repository.snapshots == []


@pytest.mark.asyncio
async def test_verified_identity_without_current_or_owned_fallback_fails_closed():
    item = candidate(status="verified", client_id="55")
    repository = FakeRepository([item])
    reader = FakeReader(history=snapshot())
    coordinator = ActivitySyncCoordinator(
        repository, reader, FakeScheduler(), clock=lambda: NOW
    )

    await coordinator.run(activity_job(NOW))

    assert repository.errors == [(item, "yclients_identity_missing", NOW)]
    assert reader.history_reads == []
    assert repository.resolutions == []


@pytest.mark.asyncio
async def test_verified_identity_fallback_provider_error_is_allowlisted():
    item = candidate(status="verified", client_id="55")
    repository = FakeRepository(
        [item],
        local=LocalBookingProof("9001", BOOKING_KEY),
    )
    reader = FakeReader(record=YclientsProjectionError("yclients_transport"))
    coordinator = ActivitySyncCoordinator(
        repository, reader, FakeScheduler(), clock=lambda: NOW
    )

    await coordinator.run(activity_job(NOW))

    assert repository.errors == [(item, "yclients_transport", NOW)]
    assert reader.history_reads == []


@pytest.mark.asyncio
async def test_unknown_provider_error_is_collapsed_to_safe_code():
    item = candidate(status="verified", client_id="55")
    repository = FakeRepository(
        [item],
        current_ids=("55",),
        resolved=ResolvedIdentity("verified", "55"),
    )
    reader = FakeReader(history=YclientsProjectionError("secret-provider-detail"))
    coordinator = ActivitySyncCoordinator(
        repository, reader, FakeScheduler(), clock=lambda: NOW
    )

    await coordinator.run(activity_job(NOW))

    assert repository.errors == [(item, "yclients_provider_error", NOW)]


@pytest.mark.asyncio
async def test_malformed_persisted_provider_id_fails_candidate_not_whole_batch():
    item = candidate()
    repository = FakeRepository(
        [item],
        local=LocalBookingProof("not-a-provider-id", BOOKING_KEY),
    )
    reader = FakeReader(record=ValueError("unsafe persisted id"))
    coordinator = ActivitySyncCoordinator(
        repository, reader, FakeScheduler(), clock=lambda: NOW
    )

    assert await coordinator.run(activity_job(NOW)) == JobResult.sent()
    assert repository.errors == [(item, "yclients_identity_missing", NOW)]


@pytest.mark.asyncio
async def test_busy_sync_skips_without_provider_or_population_work():
    item = candidate(status="verified", client_id="55")
    repository = FakeRepository([item], busy=True)
    reader = FakeReader(history=snapshot())
    scheduler = FakeScheduler()
    coordinator = ActivitySyncCoordinator(repository, reader, scheduler, clock=lambda: NOW)

    result = await coordinator.run(activity_job(NOW))

    assert result == JobResult.skipped("activity_busy")
    assert repository.prepared == 0
    assert reader.history_reads == []
    assert scheduler.jobs == [activity_job(NOW + ACTIVITY_SYNC_INTERVAL)]


@pytest.mark.asyncio
async def test_ensure_current_uses_utc_ten_minute_bucket():
    scheduler = FakeScheduler()
    coordinator = ActivitySyncCoordinator(
        FakeRepository([]), FakeReader(), scheduler, clock=lambda: NOW
    )

    await coordinator.ensure_current(NOW)

    assert scheduler.jobs == [activity_job(NOW)]
    assert scheduler.jobs[0].run_at == datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
