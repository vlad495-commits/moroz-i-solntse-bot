from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias
from uuid import UUID


DomainEvent: TypeAlias = object


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    message_id: str
    channel: str
    chat_id: str
    user_id: str
    text: str
    received_at: datetime
    correlation_id: UUID


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    status: Literal["ok", "needs_input", "escalated", "failed"]
    message: str
    next_action: str | None
    events: tuple[DomainEvent, ...]
    error_code: str | None = None
