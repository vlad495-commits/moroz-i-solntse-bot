from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    redis_url: str
    rabbitmq_url: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        database_url = env.get("DATABASE_URL", "")
        if not database_url:
            database_url = (
                f"postgresql://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}"
                f"@postgres:5432/{env['POSTGRES_DB']}"
            )
        return cls(
            database_url=database_url,
            redis_url=env.get("REDIS_URL", "redis://redis:6379/0"),
            rabbitmq_url=env["RABBITMQ_URL"],
        )
