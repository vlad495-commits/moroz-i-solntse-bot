from datetime import UTC, datetime

import pytest

from moroz.booking.time_display import format_booking_time


def test_formats_utc_as_moscow_time() -> None:
    assert (
        format_booking_time(datetime(2026, 9, 10, 9, tzinfo=UTC))
        == "10.09.2026 в 12:00"
    )
    assert format_booking_time("2026-09-10T09:00:00+00:00") == (
        "10.09.2026 в 12:00"
    )


def test_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        format_booking_time(datetime(2026, 9, 10, 12))
