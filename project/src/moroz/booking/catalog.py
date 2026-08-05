from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CatalogService:
    id: str
    title: str
    duration_minutes: int | None


@dataclass(frozen=True, slots=True)
class CatalogStaff:
    id: str
    name: str
    service_ids: tuple[str, ...]


class BookingCatalogPort(Protocol):
    async def list_services(self) -> list[CatalogService]: ...

    async def list_staff(
        self, service_ids: tuple[str, ...]
    ) -> list[CatalogStaff]: ...


def parse_id_allowlist(raw: str, name: str) -> tuple[str, ...]:
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    if (
        not values
        or any(not value.isdigit() for value in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError(f"{name} allowlist must contain unique numeric ids")
    return values
