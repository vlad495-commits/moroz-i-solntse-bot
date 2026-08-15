import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from moroz.booking.yclients_http import (
    HttpResponse,
    YclientsConfig,
    YclientsHttpClient,
    YclientsTransportError,
)


_MAX_STAFF = 100
_MAX_SERVICES_PER_STAFF = 200
_MAX_PAIRS = 5_000
_MAX_DISPLAY_LENGTH = 200
_MAX_PROVIDER_ID_LENGTH = 64
_MAX_PRICE = Decimal("99999999.99")
_MONEY_QUANTUM = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class CatalogRecord:
    service_id: str
    staff_id: str
    service_name: str
    category_name: str | None
    staff_name: str
    price_min: Decimal
    price_max: Decimal
    duration_minutes: int


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    records: tuple[CatalogRecord, ...]
    synced_at: datetime


class YclientsCatalogError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class YclientsCatalogReader:
    def __init__(
        self,
        config: YclientsConfig,
        *,
        http: YclientsHttpClient | None = None,
    ) -> None:
        self._config = config
        self._http = http or YclientsHttpClient(config)

    async def read(self, now: datetime) -> CatalogSnapshot:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        staff_data = await self._get(
            f"/api/v1/book_staff/{self._config.company_id}",
            (("without_seances", 1),),
        )
        if not isinstance(staff_data, list):
            raise YclientsCatalogError("yclients_catalog_response_shape")
        if len(staff_data) > _MAX_STAFF:
            raise YclientsCatalogError("yclients_catalog_bound")
        try:
            staff = tuple(
                item
                for value in staff_data
                if (item := _staff(value)) is not None
            )
        except (TypeError, ValueError) as error:
            raise YclientsCatalogError("yclients_catalog_response_shape") from error

        records: list[CatalogRecord] = []
        keys: set[tuple[str, str]] = set()
        for staff_id, staff_name in sorted(staff):
            data = await self._get(
                f"/api/v1/book_services/{self._config.company_id}",
                (("staff_id", int(staff_id)),),
            )
            if not isinstance(data, Mapping) or not isinstance(
                data.get("services"), list
            ):
                raise YclientsCatalogError("yclients_catalog_response_shape")
            services = data["services"]
            if len(services) > _MAX_SERVICES_PER_STAFF:
                raise YclientsCatalogError("yclients_catalog_bound")
            for value in services:
                try:
                    record = _record(value, staff_id, staff_name)
                except (InvalidOperation, TypeError, ValueError) as error:
                    raise YclientsCatalogError(
                        "yclients_catalog_response_shape"
                    ) from error
                key = (record.service_id, record.staff_id)
                if key in keys:
                    raise YclientsCatalogError("yclients_catalog_response_shape")
                keys.add(key)
                records.append(record)
                if len(records) > _MAX_PAIRS:
                    raise YclientsCatalogError("yclients_catalog_bound")
        return CatalogSnapshot(
            tuple(sorted(records, key=lambda item: (item.service_id, item.staff_id))),
            now,
        )

    async def _get(self, path: str, query=()) -> object:
        try:
            response = await self._http.request(
                "GET", path, query=query, user_auth=False
            )
        except YclientsTransportError as error:
            raise YclientsCatalogError("yclients_catalog_transport") from error
        if response.status != 200:
            raise YclientsCatalogError("yclients_catalog_http_status")
        return _response_data(response)


def _response_data(response: HttpResponse) -> object:
    try:
        envelope = json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise YclientsCatalogError("yclients_catalog_response_shape") from error
    if not isinstance(envelope, dict) or envelope.get("success") is not True:
        raise YclientsCatalogError("yclients_catalog_response_shape")
    return envelope.get("data")


def _staff(value: object) -> tuple[str, str] | None:
    if not isinstance(value, Mapping):
        raise ValueError("staff must be an object")
    bookable = value.get("bookable")
    if type(bookable) is not bool:
        raise ValueError("bookable must be boolean")
    if not bookable:
        return None
    return _provider_id(value.get("id")), _required_display(value.get("name"))


def _record(value: object, staff_id: str, staff_name: str) -> CatalogRecord:
    if not isinstance(value, Mapping):
        raise ValueError("service must be an object")
    price_min = _money(value.get("price_min"))
    price_max = _money(value.get("price_max", value.get("price_min")))
    if price_min > price_max:
        raise ValueError("price range is reversed")
    seconds = _positive_int(value.get("seance_length"))
    if seconds % 60 or seconds > 1_440 * 60:
        raise ValueError("duration is outside bounds")
    return CatalogRecord(
        service_id=_provider_id(value.get("id")),
        staff_id=staff_id,
        service_name=_required_display(value.get("title")),
        category_name=_category(value.get("category")),
        staff_name=staff_name,
        price_min=price_min,
        price_max=price_max,
        duration_minutes=seconds // 60,
    )


def _category(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("category must be an object")
    return _display(value.get("title"))


def _display(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("display text must be a string")
    return "".join(char for char in value if char.isprintable()).strip()[
        :_MAX_DISPLAY_LENGTH
    ] or None


def _required_display(value: object) -> str:
    parsed = _display(value)
    if parsed is None:
        raise ValueError("display text is required")
    return parsed


def _provider_id(value: object) -> str:
    parsed = _positive_int(value)
    result = str(parsed)
    if result != str(value) or len(result) > _MAX_PROVIDER_ID_LENGTH:
        raise ValueError("provider id is malformed")
    return result


def _positive_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("positive integer required")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("positive integer required") from error
    if parsed <= 0 or str(parsed) != str(value):
        raise ValueError("positive integer required")
    return parsed


def _money(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("money is malformed")
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0 or parsed > _MAX_PRICE:
        raise ValueError("money is outside bounds")
    quantized = parsed.quantize(_MONEY_QUANTUM)
    if quantized != parsed:
        raise ValueError("money has excess precision")
    return quantized
