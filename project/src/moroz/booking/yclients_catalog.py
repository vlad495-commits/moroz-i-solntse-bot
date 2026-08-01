import json
from collections.abc import Sequence

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
        items = _service_items(await self._read(
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
        selected = _selected_services(
            service_ids,
            self._service_allowlist,
        )
        items = _items(await self._read(
            f"/api/v1/book_staff/{self._company_id}",
            query=[("service_ids[]", value) for value in selected],
        ))
        staff = [_parse_staff(item, selected) for item in items]
        return [
            member
            for member, bookable in staff
            if bookable and member.id in self._staff_allowlist
        ]

    async def _read(
        self,
        path: str,
        *,
        query: Sequence[tuple[str, object]] = (),
    ) -> object:
        try:
            response = await self._client.request("GET", path, query=query)
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


def _service_items(data: object) -> list[dict[str, object]]:
    if not isinstance(data, dict) or "services" not in data:
        raise BookingTemporaryError()
    return _items(data["services"])


def _parse_service(item: dict[str, object]) -> CatalogService:
    return CatalogService(
        _provider_id(item.get("id")),
        _required_text(item.get("title")),
        _duration_minutes(item.get("duration")),
    )


def _parse_staff(
    item: dict[str, object],
    selected: tuple[str, ...],
) -> tuple[CatalogStaff, bool]:
    bookable = item.get("bookable")
    if type(bookable) is not bool:
        raise BookingTemporaryError()
    return (
        CatalogStaff(
            _provider_id(item.get("id")),
            _required_text(item.get("name")),
            selected,
        ),
        bookable,
    )


def _selected_services(
    values: tuple[str, ...],
    allowlist: frozenset[str],
) -> tuple[str, ...]:
    selected = tuple(_provider_id(value) for value in values)
    if (
        not selected
        or len(set(selected)) != len(selected)
        or any(value not in allowlist for value in selected)
    ):
        raise BookingTemporaryError()
    return selected


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
