from moroz.common.config import Settings


def test_settings_build_database_url_from_postgres_parts():
    settings = Settings.from_env({
        "POSTGRES_USER": "app",
        "POSTGRES_PASSWORD": "secret",
        "POSTGRES_DB": "moroz",
    })
    assert settings.database_url == "postgresql://app:secret@postgres:5432/moroz"
    assert settings.rabbitmq_url == "amqp://guest:guest@rabbitmq:5672/"
