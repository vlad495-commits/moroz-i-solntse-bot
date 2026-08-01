from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


IntentRoute = Literal[
    "booking_create",
    "booking_reschedule",
    "booking_cancel",
    "faq",
    "other",
    "complaint",
    "medical_risk",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class IntentVerdict:
    route: IntentRoute
    confidence: float
