import importlib

import pytest

from moroz.common.config import Settings, database_url_from_env


def test_settings_build_database_url_from_postgres_parts():
    settings = Settings.from_env({
        "POSTGRES_USER": "app",
        "POSTGRES_PASSWORD": "secret",
        "POSTGRES_DB": "moroz",
        "RABBITMQ_URL": "required-test-url",
    })
    assert settings.database_url == "postgresql://app:secret@postgres:5432/moroz"
    assert settings.rabbitmq_url == "required-test-url"


def test_settings_require_explicit_rabbitmq_url():
    with pytest.raises(KeyError, match="RABBITMQ_URL"):
        Settings.from_env({
            "POSTGRES_USER": "app",
            "POSTGRES_PASSWORD": "secret",
            "POSTGRES_DB": "moroz",
        })


def test_database_url_fallback_percent_encodes_reserved_parts():
    env = {
        "POSTGRES_USER": "app@team",
        "POSTGRES_PASSWORD": "p@:/%",
        "POSTGRES_DB": "moroz/db",
    }

    assert database_url_from_env(env) == (
        "postgresql://app%40team:p%40%3A%2F%25@postgres:5432/moroz%2Fdb"
    )


def test_explicit_database_url_is_preferred_byte_for_byte():
    explicit = "postgresql://literal%2Fuser:literal%25pass@db:5432/name?x=%2F"

    assert database_url_from_env({"DATABASE_URL": explicit}) == explicit


@pytest.mark.parametrize("module_name", ["config", "database"])
def test_bot_and_admin_use_encoded_shared_database_fallback(monkeypatch, module_name):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_USER", "app@team")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@:/%")
    monkeypatch.setenv("POSTGRES_DB", "moroz/db")

    module = importlib.reload(importlib.import_module(module_name))

    assert module.DATABASE_URL == (
        "postgresql://app%40team:p%40%3A%2F%25@postgres:5432/moroz%2Fdb"
    )


def test_booking_config_parses_allowlists_and_defaults(monkeypatch):
    monkeypatch.setenv("BOOKING_INTERACTIONS_ENABLED", "true")
    monkeypatch.setenv("BOOKING_MODE", "mock")
    monkeypatch.setenv("YCLIENTS_SERVICE_ALLOWLIST", "17, 29")
    monkeypatch.setenv("YCLIENTS_STAFF_ALLOWLIST", "7, 8")
    monkeypatch.setenv("BOOKING_HORIZON_DAYS", "14")
    monkeypatch.setenv("BOOKING_CONFIRMATION_TTL_SECONDS", "1800")
    monkeypatch.setenv("BOOKING_ROUTER_CONFIDENCE", "0.80")

    config = importlib.reload(importlib.import_module("config"))

    assert config.BOOKING_MODE == "mock"
    assert config.BOOKING_INTERACTIONS_ENABLED is True
    assert config.YCLIENTS_SERVICE_ALLOWLIST == ("17", "29")
    assert config.YCLIENTS_STAFF_ALLOWLIST == ("7", "8")
    assert config.BOOKING_HORIZON_DAYS == 14
    assert config.BOOKING_CONFIRMATION_TTL_SECONDS == 1800
    assert config.BOOKING_ROUTER_CONFIDENCE == 0.80


def test_booking_settings_defaults_are_safe(monkeypatch):
    for name in (
        "BOOKING_MODE",
        "BOOKING_INTERACTIONS_ENABLED",
        "YCLIENTS_SERVICE_ALLOWLIST",
        "YCLIENTS_STAFF_ALLOWLIST",
        "BOOKING_HORIZON_DAYS",
        "BOOKING_CONFIRMATION_TTL_SECONDS",
        "BOOKING_ROUTER_CONFIDENCE",
    ):
        monkeypatch.delenv(name, raising=False)

    config = importlib.reload(importlib.import_module("config"))

    assert config.BOOKING_MODE == "disabled"
    assert config.BOOKING_INTERACTIONS_ENABLED is False
    assert config.YCLIENTS_SERVICE_ALLOWLIST == ()
    assert config.YCLIENTS_STAFF_ALLOWLIST == ()
    assert config.BOOKING_HORIZON_DAYS == 14
    assert config.BOOKING_CONFIRMATION_TTL_SECONDS == 1800
    assert config.BOOKING_ROUTER_CONFIDENCE == 0.80


@pytest.mark.parametrize(
    ("mode", "services", "staff"),
    [
        (mode, services, staff)
        for mode in ("mock", "real")
        for services, staff in (
            ("", "7"), ("17,17", "7"), ("17", ""), ("17", "7,7")
        )
    ],
)
def test_booking_config_rejects_empty_or_duplicate_active_allowlists(
    monkeypatch, mode, services, staff
):
    monkeypatch.setenv("BOOKING_INTERACTIONS_ENABLED", "true")
    monkeypatch.setenv("BOOKING_MODE", mode)
    monkeypatch.setenv("YCLIENTS_SERVICE_ALLOWLIST", services)
    monkeypatch.setenv("YCLIENTS_STAFF_ALLOWLIST", staff)

    with pytest.raises(ValueError, match="allowlist must contain unique numeric ids"):
        importlib.reload(importlib.import_module("config"))


def test_disabled_booking_gate_ignores_all_stale_booking_settings(monkeypatch):
    monkeypatch.setenv("BOOKING_INTERACTIONS_ENABLED", "false")
    monkeypatch.setenv("BOOKING_MODE", "real")
    monkeypatch.setenv("YCLIENTS_SERVICE_ALLOWLIST", "broken")
    monkeypatch.setenv("YCLIENTS_STAFF_ALLOWLIST", "also-broken")
    monkeypatch.setenv("BOOKING_HORIZON_DAYS", "broken")
    monkeypatch.setenv("BOOKING_CONFIRMATION_TTL_SECONDS", "broken")
    monkeypatch.setenv("BOOKING_ROUTER_CONFIDENCE", "broken")

    config = importlib.reload(importlib.import_module("config"))

    assert config.BOOKING_MODE == "disabled"
    assert config.YCLIENTS_SERVICE_ALLOWLIST == ()
    assert config.YCLIENTS_STAFF_ALLOWLIST == ()
    assert config.BOOKING_HORIZON_DAYS == 14
    assert config.BOOKING_CONFIRMATION_TTL_SECONDS == 1800
    assert config.BOOKING_ROUTER_CONFIDENCE == 0.80


@pytest.mark.parametrize("value", ["", "TRUE", "False", "1", "yes", " true "])
def test_booking_interactions_enabled_rejects_non_strict_booleans(
    monkeypatch, value
):
    monkeypatch.setenv("BOOKING_INTERACTIONS_ENABLED", value)

    with pytest.raises(
        ValueError,
        match="BOOKING_INTERACTIONS_ENABLED must be true or false",
    ):
        importlib.reload(importlib.import_module("config"))
