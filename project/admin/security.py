"""Small admin security helpers: password hashes, TOTP and CSRF tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time

_HASH_NAME = "sha256"
_PBKDF2_ITERATIONS = 260_000
_SALT_BYTES = 16
_TOTP_STEP_SECONDS = 30
_TOTP_DIGITS = 6


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return a self-describing PBKDF2 password hash."""
    if salt is None:
        salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        _HASH_NAME,
        password.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
    )
    return "pbkdf2_{name}${iterations}${salt}${digest}".format(
        name=_HASH_NAME,
        iterations=_PBKDF2_ITERATIONS,
        salt=base64.urlsafe_b64encode(salt).decode("ascii"),
        digest=base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(encoded_hash: str, password: str) -> bool:
    """Verify a password against a hash produced by `hash_password`."""
    try:
        algorithm, iterations, encoded_salt, expected = encoded_hash.split("$", 3)
        prefix, hash_name = algorithm.split("_", 1)
        if prefix != "pbkdf2":
            return False
        salt = base64.urlsafe_b64decode(encoded_salt.encode("ascii"))
        expected_digest = base64.urlsafe_b64decode(expected.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            hash_name,
            password.encode("utf-8"),
            salt,
            int(iterations),
            dklen=len(expected_digest),
        )
    except (ValueError, TypeError, LookupError):
        return False
    return hmac.compare_digest(actual, expected_digest)


def _totp_code(secret: str, for_time: int) -> str:
    key = base64.b32decode(secret.upper(), casefold=True)
    counter = for_time // _TOTP_STEP_SECONDS
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** _TOTP_DIGITS)).zfill(_TOTP_DIGITS)


def verify_totp(
    secret: str,
    code: str,
    *,
    now: int | None = None,
    window: int = 1,
) -> bool:
    """Verify a 6-digit TOTP code with a small clock-skew window."""
    if now is None:
        now = int(time.time())
    normalized = code.strip()
    if len(normalized) != _TOTP_DIGITS or not normalized.isdigit():
        return False
    try:
        offsets = range(-window, window + 1)
        return any(
            hmac.compare_digest(
                _totp_code(secret, now + offset * _TOTP_STEP_SECONDS),
                normalized,
            )
            for offset in offsets
        )
    except (ValueError, TypeError):
        return False


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)
