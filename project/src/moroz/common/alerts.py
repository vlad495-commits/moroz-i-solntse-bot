"""Alert routing with Redis cooldown and PII redaction."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Protocol


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"\+?(?:\d[\s().-]*){9,}\d")


class RedisCooldown(Protocol):
    async def set(self, key: str, value: str, *, ex: int, nx: bool) -> object: ...


AlertSender = Callable[[str, str], Awaitable[None]]


def redact_pii(text: str) -> str:
    text = EMAIL_RE.sub("[email]", text)
    return PHONE_RE.sub("[phone]", text)


class AlertRouter:
    def __init__(
        self,
        redis: RedisCooldown,
        sender: AlertSender,
        *,
        technical_chat_id: str,
        business_chat_id: str = "",
        cooldown_seconds: int = 300,
    ) -> None:
        self._redis = redis
        self._sender = sender
        self._technical_chat_id = technical_chat_id
        self._business_chat_id = business_chat_id
        self._cooldown_seconds = cooldown_seconds

    async def emit(
        self,
        *,
        code: str,
        subject: str,
        severity: str,
        text: str,
        business_critical: bool = False,
    ) -> bool:
        key = f"alert:{code}:{subject}"
        acquired = await self._redis.set(
            key,
            "1",
            ex=self._cooldown_seconds,
            nx=True,
        )
        if not acquired:
            return False

        message = f"[{severity}] {code}/{subject}: {redact_pii(text)}"
        recipients = [self._technical_chat_id]
        if business_critical and self._business_chat_id:
            recipients.append(self._business_chat_id)
        for chat_id in recipients:
            await self._sender(chat_id, message)
        return True
