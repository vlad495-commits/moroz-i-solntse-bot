from dataclasses import dataclass

from moroz.booking.catalog import CatalogService, CatalogStaff


@dataclass(frozen=True, slots=True)
class MockBookingCatalog:
    services: tuple[CatalogService, ...]
    staff: tuple[CatalogStaff, ...]

    async def list_services(self) -> list[CatalogService]:
        return list(self.services)

    async def list_staff(
        self, service_ids: tuple[str, ...]
    ) -> list[CatalogStaff]:
        selected = set(service_ids)
        return [
            member
            for member in self.staff
            if selected.issubset(member.service_ids)
        ]
