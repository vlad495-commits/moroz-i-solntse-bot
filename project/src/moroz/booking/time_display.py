from datetime import datetime
from zoneinfo import ZoneInfo


MOSCOW = ZoneInfo("Europe/Moscow")


def format_booking_time(value: datetime | str) -> str:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("booking time must be timezone-aware")
    return parsed.astimezone(MOSCOW).strftime("%d.%m.%Y в %H:%M")
