from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from moroz.booking.models import (
    BookingOutcomeUnknown,
    BookingScenario,
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
            if await self._repository.has_successful_reconciliation(scenario.id):
                return ReconciliationResult(
                    "confirmed", "booking_reconciled_exact_match"
                )
            return self._open_result("booking_reconciliation_not_eligible")
        if (
            scenario.phase != "escalated"
            or scenario.error_code != "booking_outcome_unknown"
        ):
            return self._open_result("booking_reconciliation_not_eligible")
        expected_booking_key = self._expected_booking_key(scenario)
        if expected_booking_key is None:
            return self._open_result("booking_reconciliation_not_eligible")
        try:
            matches = await self._lookup.find_by_booking_key(expected_booking_key)
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
            expected_booking_key,
        )
        if not resolved:
            return self._open_result("booking_reconciliation_mismatch")
        return ReconciliationResult(
            "confirmed", "booking_reconciled_exact_match"
        )

    @staticmethod
    def _open_result(reason_code: str) -> ReconciliationResult:
        return ReconciliationResult("escalated", reason_code)

    @staticmethod
    def _expected_booking_key(scenario: BookingScenario) -> UUID | None:
        if scenario.kind == "create":
            return scenario.id if scenario.id.int != 0 else None
        raw_booking_key = scenario.state.get("booking_key")
        if not isinstance(raw_booking_key, str) or not raw_booking_key:
            return None
        try:
            booking_key = UUID(raw_booking_key)
        except ValueError:
            return None
        return booking_key if booking_key.int != 0 else None
