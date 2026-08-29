"""Fail-closed production environment validation."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from urllib.parse import urlparse


REQUIRED = (
    "PUBLIC_DOMAIN",
    "PUBLIC_BASE_URL",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "REDIS_PASSWORD",
    "REDIS_URL",
    "RABBITMQ_USER",
    "RABBITMQ_PASSWORD",
    "RABBITMQ_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "ADMIN_SESSION_SECRET",
    "ADMIN_COOKIE_SECURE",
    "YCLIENTS_PARTNER_TOKEN",
    "YCLIENTS_USER_TOKEN",
    "YCLIENTS_COMPANY_ID",
    "YCLIENTS_BASE_URL",
    "YCLIENTS_CATALOG_GROUNDING_ENABLED",
    "BACKUP_ENCRYPTION_KEY",
)

DEFAULT_SESSION_SECRETS = {
    "change-me-in-production",
    "change-me-min-32-chars-please",
}

SENSITIVE = (
    "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
    "RABBITMQ_PASSWORD",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "ADMIN_PASSWORD",
    "ADMIN_SESSION_SECRET",
    "YCLIENTS_PARTNER_TOKEN",
    "YCLIENTS_USER_TOKEN",
    "BACKUP_ENCRYPTION_KEY",
)


def validate(env: Mapping[str, str]) -> list[str]:
    errors: list[str] = []
    for name in REQUIRED:
        if not (env.get(name) or "").strip():
            errors.append(f"{name} is required")

    public_base_url = (env.get("PUBLIC_BASE_URL") or "").strip()
    if public_base_url and not public_base_url.startswith("https://"):
        errors.append("PUBLIC_BASE_URL must start with https://")
    public_domain = (env.get("PUBLIC_DOMAIN") or "").strip()
    if public_base_url and public_domain and urlparse(public_base_url).hostname != public_domain:
        errors.append("PUBLIC_BASE_URL host must match PUBLIC_DOMAIN")

    if (env.get("ADMIN_USERNAME") or "").strip() == "admin":
        errors.append("ADMIN_USERNAME must not be the default admin user")
    if (env.get("ADMIN_PASSWORD") or "").strip() == "admin":
        errors.append("ADMIN_PASSWORD must not be the default admin password")

    session_secret = (env.get("ADMIN_SESSION_SECRET") or "").strip()
    if session_secret in DEFAULT_SESSION_SECRETS:
        errors.append("ADMIN_SESSION_SECRET must not use a default value")
    if session_secret and len(session_secret) < 32:
        errors.append("ADMIN_SESSION_SECRET must be at least 32 characters")
    if (env.get("ADMIN_COOKIE_SECURE") or "").strip().lower() != "true":
        errors.append("ADMIN_COOKIE_SECURE must be true in production")
    if (
        (env.get("YCLIENTS_CATALOG_GROUNDING_ENABLED") or "")
        .strip()
        .lower()
        != "true"
    ):
        errors.append(
            "YCLIENTS_CATALOG_GROUNDING_ENABLED must be true in production"
        )
    webhook_secret = (env.get("TELEGRAM_WEBHOOK_SECRET") or "").strip()
    if webhook_secret and len(webhook_secret) < 16:
        errors.append("TELEGRAM_WEBHOOK_SECRET must be at least 16 characters")

    for name in SENSITIVE:
        value = (env.get(name) or "").strip()
        if value.startswith("replace-with-"):
            errors.append(f"{name} must not use a placeholder value")

    return errors


def main() -> int:
    errors = validate(os.environ)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("production env validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
