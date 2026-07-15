from dataclasses import dataclass
from typing import Mapping
from urllib.parse import quote


def database_url_from_env(
    env: Mapping[str, str], *, required: bool = True
) -> str:
    explicit = env.get("DATABASE_URL", "")
    if explicit:
        return explicit
    if not required and not all(
        env.get(key) for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
    ):
        return ""
    user = quote(env["POSTGRES_USER"], safe="")
    password = quote(env["POSTGRES_PASSWORD"], safe="")
    database = quote(env["POSTGRES_DB"], safe="")
    return f"postgresql://{user}:{password}@postgres:5432/{database}"


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    redis_url: str
    rabbitmq_url: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        return cls(
            database_url=database_url_from_env(env),
            redis_url=env.get("REDIS_URL", "redis://redis:6379/0"),
            rabbitmq_url=env["RABBITMQ_URL"],
        )
