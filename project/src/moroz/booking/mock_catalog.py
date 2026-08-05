from dataclasses import dataclass

from moroz.booking.catalog import CatalogService, CatalogStaff


@dataclass(frozen=True, slots=True)
class MockBookingCatalog:
    services: tuple[CatalogService, ...]
    staff: tuple[CatalogStaff, ...]
    service_allowlist: tuple[str, ...]
    staff_allowlist: tuple[str, ...]

    async def list_services(self) -> list[CatalogService]:
        return [
            service
            for service in self.services
            if service.id in self.service_allowlist
        ]

    async def list_staff(
        self, service_ids: tuple[str, ...]
    ) -> list[CatalogStaff]:
        selected = set(service_ids)
        if not selected.issubset(self.service_allowlist):
            return []

        result = []
        for member in self.staff:
            service_ids = tuple(
                service_id
                for service_id in member.service_ids
                if service_id in self.service_allowlist
            )
            if (
                member.id in self.staff_allowlist
                and selected.issubset(service_ids)
            ):
                result.append(CatalogStaff(member.id, member.name, service_ids))
        return result
