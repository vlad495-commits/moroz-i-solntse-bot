from uuid import UUID

from moroz.booking.models import ExternalBooking
from moroz.common.db import Database
from moroz.messaging.repository import MessageRepository


class LocalBookingPort:
    def __init__(self, database: Database):
        self._database = database

    async def get_booking(self, booking_key: UUID) -> ExternalBooking | None:
        async with self._database.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT external_id, customer_id, booking_key, slot_id,
                       starts_at, status
                FROM bookings
                WHERE booking_key = $1
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                booking_key,
            )
        if row is None:
            return None
        return ExternalBooking(
            external_id=row["external_id"],
            customer_id=row["customer_id"],
            booking_key=row["booking_key"],
            slot_id=row["slot_id"],
            starts_at=row["starts_at"],
            status=row["status"],
        )


class NotificationOutbox:
    def __init__(
        self,
        repository: MessageRepository,
        *,
        staff_chat_id: str = "",
    ):
        self._repository = repository
        self._staff_chat_id = staff_chat_id.strip()

    async def reminder(self, booking: ExternalBooking, kind: str) -> None:
        await self._send_client(
            booking,
            _reminder_text(booking, kind),
            f"notification:{booking.booking_key}:{booking.starts_at.isoformat()}:{kind}",
        )

    async def client_waiting(self, booking: ExternalBooking) -> None:
        await self._send_client(
            booking,
            "Вижу, что запись уже началась. Если Вы на месте, администратор скоро поможет.",
            f"notification:{booking.booking_key}:{booking.starts_at.isoformat()}:client_waiting",
        )

    async def staff_no_show(self, booking: ExternalBooking) -> None:
        await self._send_staff(
            f"Клиент не пришёл на запись {booking.external_id}.",
            f"staff:{booking.booking_key}:{booking.starts_at.isoformat()}:no_show",
        )

    async def staff_status_unknown(
        self,
        booking: ExternalBooking,
        status: str,
    ) -> None:
        await self._send_staff(
            f"Нужна проверка записи {booking.external_id}: статус {status}.",
            f"staff:{booking.booking_key}:{booking.starts_at.isoformat()}:status_unknown",
        )

    async def feedback_request(self, customer_id: str, booking_key: UUID) -> None:
        await self._repository.enqueue_outbound(
            channel="telegram",
            chat_id=customer_id,
            text=(
                "Спасибо, что были у нас. Оцените, пожалуйста, процедуру и сервис "
                "от 1 до 5 одним сообщением."
            ),
            idempotency_key=f"feedback_request:{booking_key}:{customer_id}",
        )

    async def _send_client(
        self,
        booking: ExternalBooking,
        text: str,
        idempotency_key: str,
    ) -> None:
        await self._repository.enqueue_outbound(
            channel="telegram",
            chat_id=booking.customer_id,
            text=text,
            idempotency_key=idempotency_key,
        )

    async def _send_staff(self, text: str, idempotency_key: str) -> None:
        if not self._staff_chat_id:
            raise RuntimeError("STAFF_TELEGRAM_CHAT_ID is not configured")
        await self._repository.enqueue_outbound(
            channel="telegram",
            chat_id=self._staff_chat_id,
            text=text,
            idempotency_key=idempotency_key,
        )


def _reminder_text(booking: ExternalBooking, kind: str) -> str:
    if kind == "booking_created":
        return f"Запись подтверждена на {booking.starts_at.isoformat()}."
    if kind == "day_before":
        return f"Напоминаем: завтра у Вас запись на {booking.starts_at.isoformat()}."
    if kind in {"morning", "hour_before", "morning_hour_before"}:
        return f"Напоминаем о записи сегодня: {booking.starts_at.isoformat()}."
    return f"Напоминание о записи: {booking.starts_at.isoformat()}."
