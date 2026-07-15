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
