import pytest

from moroz.booking.telegram import normalize_russian_phone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("8 999 123-45-67", "+79991234567"),
        ("7 (999) 123-45-67", "+79991234567"),
        ("9991234567", "+79991234567"),
        ("+7 999 123 45 67", "+79991234567"),
        ("123", None),
        ("+1 999 123 45 67", None),
    ],
)
def test_normalize_russian_phone(raw, expected):
    assert normalize_russian_phone(raw) == expected
