from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    update_id: str
    message_id: str
    channel: str
    chat_id: str
    user_id: str
    text: str
    received_at: datetime
    correlation_id: UUID
    kind: Literal["text", "callback", "contact"] = "text"
    data: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    id: UUID
    channel: str
    chat_id: str
    text: str
    delivery_options: dict[str, object]
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    status: Literal["ok", "needs_input", "escalated", "failed"]
    message: str
    next_action: str | None
    events: tuple[object, ...]
    error_code: str | None = None
