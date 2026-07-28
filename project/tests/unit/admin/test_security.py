import base64
import importlib


security = importlib.import_module("security")


def test_password_hash_verifies_and_rejects_wrong_password():
    encoded = security.hash_password("correct horse")

    assert security.verify_password(encoded, "correct horse")
    assert not security.verify_password(encoded, "wrong")


def test_password_hash_uses_unique_salt_by_default():
    first = security.hash_password("same password")
    second = security.hash_password("same password")

    assert first != second
    assert security.verify_password(first, "same password")
    assert security.verify_password(second, "same password")


def test_password_hash_accepts_fixed_salt_for_repeatable_tests():
    salt = b"1234567890123456"

    assert security.hash_password("pw", salt=salt) == security.hash_password("pw", salt=salt)


def test_totp_accepts_current_code_and_rejects_wrong_code():
    secret = base64.b32encode(b"hello world").decode("ascii")
    code = security._totp_code(secret, 59)

    assert security.verify_totp(secret, code, now=59, window=0)
    assert not security.verify_totp(secret, "000000", now=59, window=0)


def test_totp_accepts_adjacent_window():
    secret = base64.b32encode(b"hello world").decode("ascii")
    previous_code = security._totp_code(secret, 29)

    assert security.verify_totp(secret, previous_code, now=30, window=1)
    assert not security.verify_totp(secret, previous_code, now=90, window=1)


def test_csrf_token_is_url_safe_and_random():
    first = security.new_csrf_token()
    second = security.new_csrf_token()

    assert first
    assert second
    assert first != second

