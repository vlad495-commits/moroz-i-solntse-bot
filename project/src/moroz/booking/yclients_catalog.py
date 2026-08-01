import json

from moroz.booking.catalog import (
    BookingCatalogPort,
    CatalogService,
    CatalogStaff,
)
from moroz.booking.models import BookingTemporaryError
from moroz.booking.yclients_http import (
    HttpResponse,
    YclientsHttpClient,
    YclientsTransportError,
)


class YclientsCatalogAdapter(BookingCatalogPort):
    def __init__(
        self,
        client: YclientsHttpClient,
        company_id: str,
        service_allowlist: tuple[str, ...],
        staff_allowlist: tuple[str, ...],
    ) -> None:
        self._client = client
        self._company_id = company_id
        self._service_allowlist = frozenset(service_allowlist)
        self._staff_allowlist = frozenset(staff_allowlist)

    async def list_services(self) -> list[CatalogService]:
        items = _items(await self._read(
            f"/api/v1/book_services/{self._company_id}"
        ))
        services = [_parse_service(item) for item in items]
        return [
            service
            for service in services
            if service.id in self._service_allowlist
        ]

    async def list_staff(
        self, service_ids: tuple[str, ...]
    ) -> list[CatalogStaff]:
        items = _items(await self._read(
            f"/api/v1/book_staff/{self._company_id}"
        ))
        selected = set(service_ids)
        result = []
        for staff in (_parse_staff(item) for item in items):
            allowed_services = tuple(
                service_id
                for service_id in staff.service_ids
                if service_id in self._service_allowlist
            )
            if (
                staff.id in self._staff_allowlist
                and selected.issubset(allowed_services)
            ):
                result.append(CatalogStaff(
                    staff.id,
                    staff.name,
                    allowed_services,
                ))
        return result

    async def _read(self, path: str) -> object:
        try:
            response = await self._client.request("GET", path)
        except YclientsTransportError:
            raise BookingTemporaryError() from None
        if response.status != 200:
            raise BookingTemporaryError()
        return _envelope(response)


def _envelope(response: HttpResponse) -> object:
    try:
        value = json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise BookingTemporaryError() from None
    if (
        not isinstance(value, dict)
        or value.get("success") is not True
        or "data" not in value
    ):
        raise BookingTemporaryError()
    return value["data"]


def _items(data: object) -> list[dict[str, object]]:
    if not isinstance(data, list) or any(
        not isinstance(item, dict) for item in data
    ):
        raise BookingTemporaryError()
    return data


def _parse_service(item: dict[str, object]) -> CatalogService:
    return CatalogService(
        _provider_id(item.get("id")),
        _required_text(item.get("title")),
        _duration_minutes(item.get("duration")),
    )


def _parse_staff(item: dict[str, object]) -> CatalogStaff:
    raw_services = item.get("services")
    if not isinstance(raw_services, list) or not raw_services:
        raise BookingTemporaryError()
    service_ids = tuple(sorted(
        {_provider_id(value) for value in raw_services},
        key=int,
    ))
    return CatalogStaff(
        _provider_id(item.get("id")),
        _required_text(item.get("name")),
        service_ids,
    )


def _provider_id(value: object) -> str:
    if isinstance(value, bool):
        raise BookingTemporaryError()
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise BookingTemporaryError() from None
    if parsed <= 0 or str(parsed) != str(value):
        raise BookingTemporaryError()
    return str(parsed)


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BookingTemporaryError()
    return value.strip()


def _duration_minutes(value: object) -> int:
    if type(value) is not int or value <= 0 or value % 60:
        raise BookingTemporaryError()
    return value // 60
