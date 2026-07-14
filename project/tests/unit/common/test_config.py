import pytest

from moroz.common.config import Settings


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
