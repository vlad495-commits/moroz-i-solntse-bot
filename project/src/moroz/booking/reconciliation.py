from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from moroz.booking.models import (
    BookingOutcomeUnknown,
    BookingTemporaryError,
    ExternalBooking,
)
from moroz.booking.repository import BookingRepository


class BookingLookupPort(Protocol):
    async def find_by_booking_key(
        self,
        booking_key: UUID,
    ) -> list[ExternalBooking]: ...


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    status: Literal["confirmed", "escalated"]
    reason_code: str


class BookingReconciler:
    def __init__(
        self,
        lookup: BookingLookupPort,
        repository: BookingRepository,
    ) -> None:
        self._lookup = lookup
        self._repository = repository

    async def reconcile(self, scenario_id: UUID) -> ReconciliationResult:
        scenario = await self._repository.get_scenario(scenario_id)
        if scenario is None:
            return self._open_result("booking_reconciliation_not_eligible")
        if scenario.phase == "confirmed":
            return ReconciliationResult(
                "confirmed", "booking_reconciled_exact_match"
            )
        if (
            scenario.phase != "escalated"
            or scenario.error_code != "booking_outcome_unknown"
        ):
            return self._open_result("booking_reconciliation_not_eligible")
        try:
            matches = await self._lookup.find_by_booking_key(scenario.id)
        except (BookingTemporaryError, BookingOutcomeUnknown, TimeoutError, ValueError):
            return self._open_result("booking_reconciliation_lookup_failed")
        if not isinstance(matches, list) or len(matches) != 1:
            return self._open_result("booking_reconciliation_match_count")
        match = matches[0]
        if not isinstance(match, ExternalBooking):
            return self._open_result("booking_reconciliation_malformed")
        resolved = await self._repository.resolve_reconciled_booking(
            scenario.id,
            match,
        )
        if not resolved:
            return self._open_result("booking_reconciliation_mismatch")
        return ReconciliationResult(
            "confirmed", "booking_reconciled_exact_match"
        )

    @staticmethod
    def _open_result(reason_code: str) -> ReconciliationResult:
        return ReconciliationResult("escalated", reason_code)
