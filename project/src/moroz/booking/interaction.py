from __future__ import annotations

from collections.abc import Mapping
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


@dataclass(frozen=True, slots=True)
class BookingOwner:
    channel: str
    chat_id: str
    customer_id: str

    def __post_init__(self) -> None:
        if self.channel != "telegram":
            raise ValueError("booking workflow supports Telegram only")
        if not self.chat_id or not self.customer_id:
            raise ValueError("booking owner identity is incomplete")


InteractionKind = Literal["text", "callback", "contact"]


@dataclass(frozen=True, slots=True)
class Interaction:
    kind: InteractionKind
    owner: BookingOwner
    idempotency_key: str
    text_value: str | None = None
    callback_data: str | None = None
    contact_user_id: str | None = None
    phone_number: str | None = None
    personal_data_processing_allowed: bool = False

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            raise ValueError("interaction idempotency key is required")
        if self.kind == "text" and self.text_value is None:
            raise ValueError("text interaction requires text")
        if self.kind == "callback" and self.callback_data is None:
            raise ValueError("callback interaction requires callback data")
        if self.kind == "contact" and (
            self.contact_user_id is None or self.phone_number is None
        ):
            raise ValueError("contact interaction requires owner and phone")

    @classmethod
    def text(
        cls,
        owner: BookingOwner,
        idempotency_key: str,
        value: str,
    ) -> "Interaction":
        return cls("text", owner, idempotency_key, text_value=value)

    @classmethod
    def callback(
        cls,
        owner: BookingOwner,
        idempotency_key: str,
        callback_data: str,
    ) -> "Interaction":
        return cls(
            "callback",
            owner,
            idempotency_key,
            callback_data=callback_data,
        )

    @classmethod
    def contact(
        cls,
        owner: BookingOwner,
        idempotency_key: str,
        *,
        contact_user_id: str,
        phone_number: str,
        personal_data_processing_allowed: bool = False,
    ) -> "Interaction":
        return cls(
            "contact",
            owner,
            idempotency_key,
            contact_user_id=contact_user_id,
            phone_number=phone_number,
            personal_data_processing_allowed=personal_data_processing_allowed,
        )


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("delivery option keys must be strings")
            result[key] = _plain_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("delivery options must be JSON-compatible")


@dataclass(frozen=True, slots=True)
class WorkflowReply:
    text: str
    delivery_options: dict[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("workflow reply text is required")
        plain = _plain_json(self.delivery_options)
        if not isinstance(plain, dict):
            raise ValueError("delivery options must be an object")
        object.__setattr__(self, "delivery_options", plain)

    def to_result(self) -> dict[str, object]:
        return {
            "text": self.text,
            "delivery_options": _plain_json(self.delivery_options),
        }

    @classmethod
    def from_result(cls, value: Mapping[str, object]) -> "WorkflowReply":
        if set(value) != {"text", "delivery_options"}:
            raise ValueError("workflow result has invalid fields")
        text = value["text"]
        delivery_options = value["delivery_options"]
        if not isinstance(text, str) or not isinstance(delivery_options, Mapping):
            raise ValueError("workflow result has invalid values")
        plain = _plain_json(delivery_options)
        if not isinstance(plain, dict):
            raise ValueError("delivery options must be an object")
        return cls(text, plain)
