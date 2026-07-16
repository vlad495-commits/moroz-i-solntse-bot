from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    status: Literal["ok", "needs_input", "escalated", "failed"]
    message: str
    next_action: str | None
    events: tuple[object, ...]
    error_code: str | None = None
