import os
from collections.abc import Mapping
from urllib.parse import quote


def database_url(env: Mapping[str, str] = os.environ) -> str:
    explicit = env.get("DATABASE_URL", "")
    if explicit:
        return explicit
    user = quote(env["POSTGRES_USER"], safe="")
    password = quote(env["POSTGRES_PASSWORD"], safe="")
    database = quote(env["POSTGRES_DB"], safe="")
    return f"postgresql://{user}:{password}@postgres:5432/{database}"


def sqlalchemy_database_url(env: Mapping[str, str] = os.environ) -> str:
    return database_url(env).replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
