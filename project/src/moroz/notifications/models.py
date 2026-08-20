from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from uuid import UUID


@dataclass(frozen=True)
class SchedulerJob:
    id: UUID
    kind: str
    run_at: datetime
    payload: MappingProxyType
    idempotency_key: str
    attempts: int
    booking_key: UUID | None
    booking_starts_at: datetime | None


@dataclass(frozen=True)
class PlannedSchedulerJob:
    kind: str
    run_at: datetime
    payload: MappingProxyType
    idempotency_key: str
    booking_key: UUID | None
    booking_starts_at: datetime | None


@dataclass(frozen=True)
class JobResult:
    status: str
    reason: str | None = None

    @classmethod
    def sent(cls) -> "JobResult":
        return cls("sent")

    @classmethod
    def skipped(cls, reason: str) -> "JobResult":
        return cls("skipped", reason)
