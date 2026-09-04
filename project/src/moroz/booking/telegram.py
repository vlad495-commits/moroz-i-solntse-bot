from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg

from moroz.booking.catalog import CatalogRepository, CatalogService
from moroz.booking.models import BookingIdentity, BookingScenario, Slot, SlotQuery
from moroz.booking.ports import BookingPort
from moroz.booking.repository import BookingRepository
from moroz.booking.service import BookingService


STALE_REPLY = "Эта кнопка уже неактуальна. Начните запись заново."


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class BookingReply:
    text: str
    delivery_options: dict[str, object]


def normalize_russian_phone(value: str) -> str | None:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) == 10:
        digits = f"7{digits}"
    elif len(digits) == 11 and digits.startswith("8"):
        digits = f"7{digits[1:]}"
    if len(digits) != 11 or not digits.startswith("7"):
        return None
    return f"+{digits}"


class TelegramBookingCoordinator:
    def __init__(
        self,
        repository: BookingRepository,
        catalog: CatalogRepository,
        booking_service: BookingService,
        port: BookingPort,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repository = repository
        self._catalog = catalog
        self._booking_service = booking_service
        self._port = port
        self._now = now

    async def handle(
        self,
        connection: asyncpg.Connection,
        *,
        customer_id: str,
        user_id: str,
        update_id: str,
        text: str,
        kind: str,
        data: Mapping[str, object],
    ) -> BookingReply | None:
        if kind == "callback":
            return await self._handle_callback(
                customer_id, user_id, data.get("callback_data")
            )

        scenario = await self._repository.get_active_for_customer(customer_id)
        if scenario is None:
            if kind == "text" and "мои запис" in text.casefold():
                return await self._start_management(customer_id, update_id)
            if kind != "text" or "запис" not in text.casefold():
                return None
            return await self._start(connection, customer_id, update_id)

        step = scenario.state.get("step")
        if step == "contact":
            return await self._collect_contact(
                connection, scenario, user_id, text, kind, data
            )
        if step == "name" and kind == "text":
            name = text.strip()
            if not name:
                return BookingReply("Как вас зовут?", {})
            state = self._state(scenario)
            state["customer_name"] = name
            return await self._show_confirmation(scenario, state)
        return self._render_current(scenario)

    async def _start_management(
        self, customer_id: str, update_id: str
    ) -> BookingReply:
        owned = await self._repository.list_future_owned(customer_id, self._now())
        if not owned:
            return BookingReply(
                "Здесь можно управлять только будущими записями, созданными через этот Telegram-чат. По остальным поможет администратор.",
                {},
            )
        choices = []
        for booking, raw_state in owned:
            state = self._state_item(raw_state)
            service_name = str(state.get("service_name", "Услуга"))
            staff_name = str(state.get("staff_name", "Специалист"))
            choices.append(
                {
                    "external_id": booking.external_id,
                    "booking_key": str(booking.booking_key),
                    "slot_id": booking.slot_id,
                    "starts_at": booking.starts_at.isoformat(),
                    "service_id": str(state.get("service_id", "")),
                    "service_name": service_name,
                    "staff_id": state.get("selected_staff_id"),
                    "staff_name": staff_name,
                    "staff_names": self._state_item(state.get("staff_names", {})),
                    "label": f"{booking.starts_at:%d.%m %H:%M} — {service_name}",
                }
            )
        scenario = BookingScenario(
            id=uuid4(),
            kind="create",
            phase="collecting",
            idempotency_key=f"telegram:manage:{update_id}",
            customer_id=customer_id,
            state={
                "step": "booking_management",
                "source": "telegram",
                "choices": choices,
            },
            error_code=None,
            created_at=self._now(),
            updated_at=self._now(),
        )
        await self._repository.create_scenario(scenario)
        details = "\n".join(str(choice["label"]) for choice in choices)
        return self._choice_reply(
            scenario,
            f"Ваши записи:\n{details}",
            "booking_management",
        )

    async def _start(
        self,
        connection: asyncpg.Connection,
        customer_id: str,
        update_id: str,
    ) -> BookingReply:
        services = await self._catalog.list_services(connection)
        if not services:
            return BookingReply(
                "Сейчас не могу загрузить услуги. Напишите администратору.", {}
            )
        scenario = BookingScenario(
            id=uuid4(),
            kind="create",
            phase="collecting",
            idempotency_key=f"telegram:create:{update_id}",
            customer_id=customer_id,
            state={
                "step": "service",
                "source": "telegram",
                "choices": [self._service_choice(service) for service in services],
            },
            error_code=None,
            created_at=self._now(),
            updated_at=self._now(),
        )
        try:
            await self._repository.create_scenario(scenario)
        except asyncpg.UniqueViolationError:
            active = await self._repository.get_active_for_customer(customer_id)
            if active is None:
                raise
            return self._render_current(active)
        return self._choice_reply(scenario, "Выберите услугу", "service")

    async def _handle_callback(
        self,
        customer_id: str,
        user_id: str,
        raw_callback: object,
    ) -> BookingReply:
        parsed = self._parse_callback(raw_callback)
        if parsed is None:
            return BookingReply(STALE_REPLY, {})
        scenario_id, action, index = parsed
        scenario = await self._repository.get_scenario(scenario_id)
        if scenario is None or scenario.customer_id != customer_id:
            return BookingReply(STALE_REPLY, {})
        if (
            action == "confirm"
            and index == 0
            and scenario.state.get("step") == "confirm"
        ):
            result = await self._booking_service.handle(
                scenario.id,
                confirmed=True,
            )
            return BookingReply(result.message, {})
        if (
            action == "confirm_change"
            and index == 0
            and scenario.state.get("step") == "confirm_change"
        ):
            result = await self._booking_service.handle(
                scenario.id,
                confirmed=True,
                identity=BookingIdentity(customer_id, confirmed=True),
            )
            return BookingReply(result.message, {})
        if scenario.phase != "collecting" or scenario.state.get("step") != action:
            return BookingReply(STALE_REPLY, {})
        choices = scenario.state.get("choices")
        if not isinstance(choices, tuple) or not 0 <= index < len(choices):
            return BookingReply(STALE_REPLY, {})
        choice = choices[index]
        if not isinstance(choice, Mapping):
            return BookingReply(STALE_REPLY, {})
        if action == "service":
            return await self._choose_service(scenario, choice)
        if action == "staff":
            return await self._choose_staff(scenario, choice)
        if action == "available_date":
            return await self._choose_date(scenario, choice)
        if action == "slot":
            return await self._choose_slot(scenario, choice)
        if action == "booking_management":
            return await self._choose_owned_booking(scenario, choice)
        if action == "booking_action":
            return await self._begin_change(scenario, choice)
        return BookingReply(STALE_REPLY, {})

    async def _choose_owned_booking(
        self, scenario: BookingScenario, choice: Mapping[str, object]
    ) -> BookingReply:
        state = self._state(scenario)
        state.update(
            {
                "step": "booking_action",
                "selected_booking": self._state_item(choice),
                "choices": [
                    {"operation": "reschedule", "label": "Перенести"},
                    {"operation": "cancel", "label": "Отменить"},
                ],
            }
        )
        updated = await self._checkpoint(
            scenario, state, "booking_management_selected"
        )
        return self._choice_reply(updated, "Что сделать с записью?", "booking_action")

    async def _begin_change(
        self, management: BookingScenario, choice: Mapping[str, object]
    ) -> BookingReply:
        operation = str(choice.get("operation", ""))
        if operation not in {"reschedule", "cancel"}:
            return BookingReply(STALE_REPLY, {})
        selected = management.state.get("selected_booking")
        if not isinstance(selected, Mapping):
            return BookingReply(STALE_REPLY, {})
        closed = replace(management, phase="failed", updated_at=self._now())
        await self._repository.checkpoint(closed, "booking_management_completed")
        base_state = {
            "source": "telegram",
            "external_id": str(selected["external_id"]),
            "booking_key": str(selected["booking_key"]),
            "starts_at": str(selected["starts_at"]),
            "service_id": str(selected["service_id"]),
            "service_name": str(selected["service_name"]),
            "staff_id": selected.get("staff_id"),
            "staff_name": str(selected["staff_name"]),
            "staff_names": self._state_item(selected.get("staff_names", {})),
        }
        scenario = BookingScenario(
            id=uuid4(),
            kind=operation,
            phase=("collecting" if operation == "reschedule" else "awaiting_confirmation"),
            idempotency_key=f"telegram:{operation}:{management.id.hex}",
            customer_id=management.customer_id,
            state=base_state,
            error_code=None,
            created_at=self._now(),
            updated_at=self._now(),
        )
        if operation == "cancel":
            scenario = replace(scenario, state={**base_state, "step": "confirm_change"})
            await self._repository.create_scenario(scenario)
            return BookingReply(
                f"Отменить запись на {datetime.fromisoformat(str(selected['starts_at'])):%d.%m %H:%M}?",
                self._inline_options(
                    [[("Да, отменить", self._callback(scenario, "confirm_change", 0))]]
                ),
            )
        staff_names = base_state["staff_names"]
        choices = [{"staff_id": None, "label": "Любой специалист"}]
        if isinstance(staff_names, Mapping):
            choices.extend(
                {"staff_id": staff_id, "label": label}
                for staff_id, label in staff_names.items()
            )
        scenario = replace(
            scenario,
            state={**base_state, "step": "staff", "choices": choices},
        )
        await self._repository.create_scenario(scenario)
        return self._choice_reply(scenario, "Выберите специалиста", "staff")

    async def _choose_service(
        self, scenario: BookingScenario, choice: Mapping[str, object]
    ) -> BookingReply:
        variants = choice.get("variants")
        if not isinstance(variants, tuple):
            return BookingReply(STALE_REPLY, {})
        state = self._state(scenario)
        state.update(
            {
                "step": "staff",
                "service_id": str(choice["service_id"]),
                "service_name": str(choice["label"]),
                "staff_names": {
                    str(item["staff_id"]): str(item["label"])
                    for item in variants
                    if isinstance(item, Mapping)
                },
                "choices": [
                    {"staff_id": None, "label": "Любой специалист"},
                    *[
                        {"staff_id": item["staff_id"], "label": item["label"]}
                        for item in variants
                        if isinstance(item, Mapping)
                    ],
                ],
            }
        )
        updated = await self._checkpoint(scenario, state, "booking_service_selected")
        return self._choice_reply(updated, "Выберите специалиста", "staff")

    async def _choose_staff(
        self, scenario: BookingScenario, choice: Mapping[str, object]
    ) -> BookingReply:
        now = self._now()
        query = SlotQuery(
            (str(scenario.state["service_id"]),),
            now,
            now + timedelta(days=14),
            str(choice["staff_id"]) if choice.get("staff_id") is not None else None,
        )
        slots = sorted(await self._port.list_slots(query), key=lambda item: item.starts_at)
        if not slots:
            return BookingReply(
                "Свободного времени пока нет. Выберите другого специалиста или напишите администратору.",
                self._choice_options(scenario, "staff"),
            )
        dates = []
        for slot in slots:
            value = slot.starts_at.date().isoformat()
            if value not in {item["date"] for item in dates}:
                dates.append({"date": value, "label": slot.starts_at.strftime("%d.%m")})
            if len(dates) == 7:
                break
        state = self._state(scenario)
        state.update(
            {
                "step": "available_date",
                "staff_id": choice.get("staff_id"),
                "staff_name": str(choice["label"]),
                "slot_query": {
                    "service_ids": [str(scenario.state["service_id"])],
                    "starts_after": query.starts_after.isoformat(),
                    "starts_before": query.starts_before.isoformat(),
                    "staff_id": query.staff_id,
                },
                "available_slots": [self._slot_choice(slot) for slot in slots],
                "choices": dates,
            }
        )
        updated = await self._checkpoint(scenario, state, "booking_staff_selected")
        return self._choice_reply(updated, "Выберите дату", "available_date")

    async def _choose_date(
        self, scenario: BookingScenario, choice: Mapping[str, object]
    ) -> BookingReply:
        selected_date = str(choice["date"])
        raw_slots = scenario.state.get("available_slots")
        if not isinstance(raw_slots, tuple):
            return BookingReply(STALE_REPLY, {})
        slots = [
            self._state_item(item)
            for item in raw_slots
            if isinstance(item, Mapping)
            and str(item.get("starts_at", ""))[:10] == selected_date
        ][:8]
        state = self._state(scenario)
        state.update(
            {"step": "slot", "selected_date": selected_date, "choices": slots}
        )
        updated = await self._checkpoint(scenario, state, "booking_date_selected")
        return self._choice_reply(updated, "Выберите время", "slot")

    async def _choose_slot(
        self, scenario: BookingScenario, choice: Mapping[str, object]
    ) -> BookingReply:
        state = self._state(scenario)
        state["selected_slot_id"] = str(choice["slot_id"])
        state["selected_staff_id"] = choice.get("staff_id")
        if scenario.kind == "reschedule":
            state.update(
                {"step": "confirm_change", "new_starts_at": str(choice["starts_at"])}
            )
            updated = replace(
                scenario,
                phase="awaiting_confirmation",
                state=state,
                updated_at=self._now(),
            )
            await self._repository.checkpoint(updated, "booking_reschedule_collected")
            return BookingReply(
                f"Перенести запись на {datetime.fromisoformat(str(choice['starts_at'])):%d.%m %H:%M}?",
                self._inline_options(
                    [[("Да, перенести", self._callback(updated, "confirm_change", 0))]]
                ),
            )
        state.update({"step": "contact", "starts_at": str(choice["starts_at"])})
        await self._checkpoint(scenario, state, "booking_slot_selected")
        return BookingReply(
            "Отправьте свой контакт кнопкой ниже или напишите номер телефона.",
            {
                "reply_markup": {
                    "keyboard": [[{"text": "Отправить контакт", "request_contact": True}]],
                    "resize_keyboard": True,
                    "one_time_keyboard": True,
                }
            },
        )

    async def _collect_contact(
        self,
        connection: asyncpg.Connection,
        scenario: BookingScenario,
        user_id: str,
        text: str,
        kind: str,
        data: Mapping[str, object],
    ) -> BookingReply:
        consented = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM processing_consents "
            "WHERE channel = 'telegram' AND user_id = $1)",
            user_id,
        )
        if not consented:
            return BookingReply(
                "Для записи нужно согласие на обработку персональных данных.", {}
            )
        name = ""
        if kind == "contact":
            if str(data.get("contact_user_id", "")) != user_id:
                return BookingReply("Пожалуйста, отправьте именно свой контакт.", {})
            raw_phone = str(data.get("phone_number", ""))
            name = " ".join(
                part.strip()
                for part in (
                    str(data.get("first_name", "")),
                    str(data.get("last_name", "")),
                )
                if part.strip()
            )
        else:
            raw_phone = text
        phone = normalize_russian_phone(raw_phone)
        if phone is None:
            return BookingReply("Проверьте номер телефона и отправьте его ещё раз.", {})
        state = self._state(scenario)
        state["customer_phone"] = phone
        if not name:
            state["step"] = "name"
            await self._checkpoint(scenario, state, "booking_phone_collected")
            return BookingReply("Как вас зовут?", {})
        state["customer_name"] = name
        return await self._show_confirmation(scenario, state)

    async def _show_confirmation(
        self, scenario: BookingScenario, state: dict[str, object]
    ) -> BookingReply:
        state.update({"step": "confirm", "personal_data_processing_allowed": True})
        updated = replace(
            scenario,
            phase="awaiting_confirmation",
            state=state,
            updated_at=self._now(),
        )
        await self._repository.checkpoint(updated, "booking_details_collected")
        phone = str(state["customer_phone"])
        masked = f"{phone[:2]}******{phone[-4:]}"
        starts_at = datetime.fromisoformat(str(state["starts_at"]))
        text = (
            f"Проверьте запись:\n{state['service_name']}\n"
            f"{state['staff_name']}\n{starts_at:%d.%m %H:%M}\n"
            f"{state['customer_name']}, {masked}"
        )
        return BookingReply(
            text,
            self._inline_options(
                [[("Подтвердить", self._callback(updated, "confirm", 0))]]
            ),
        )

    async def _checkpoint(
        self, scenario: BookingScenario, state: Mapping[str, object], event: str
    ) -> BookingScenario:
        updated = replace(scenario, state=state, updated_at=self._now())
        await self._repository.checkpoint(updated, event)
        return updated

    def _choice_reply(
        self, scenario: BookingScenario, text: str, action: str
    ) -> BookingReply:
        choices = scenario.state.get("choices")
        rows = [
            [(str(choice["label"]), self._callback(scenario, action, index))]
            for index, choice in enumerate(choices if isinstance(choices, tuple) else ())
            if isinstance(choice, Mapping)
        ]
        return BookingReply(text, self._inline_options(rows))

    def _choice_options(self, scenario: BookingScenario, action: str):
        return self._choice_reply(scenario, "", action).delivery_options

    def _render_current(self, scenario: BookingScenario) -> BookingReply:
        labels = {
            "service": "Выберите услугу",
            "staff": "Выберите специалиста",
            "available_date": "Выберите дату",
            "slot": "Выберите время",
            "contact": "Отправьте номер телефона.",
            "name": "Как вас зовут?",
            "confirm": "Подтвердите запись кнопкой выше.",
        }
        step = str(scenario.state.get("step", ""))
        return BookingReply(labels.get(step, STALE_REPLY), {})

    @staticmethod
    def _service_choice(service: CatalogService) -> dict[str, object]:
        return {
            "service_id": service.service_id,
            "label": service.service_name,
            "variants": [
                {"staff_id": variant.staff_id, "label": variant.staff_name}
                for variant in service.variants
            ],
        }

    @staticmethod
    def _slot_choice(slot: Slot) -> dict[str, object]:
        return {
            "slot_id": slot.id,
            "starts_at": slot.starts_at.isoformat(),
            "staff_id": slot.staff_id,
            "label": slot.starts_at.strftime("%H:%M"),
        }

    @staticmethod
    def _state_item(value: Mapping[str, object]) -> dict[str, object]:
        return {key: item for key, item in value.items()}

    @staticmethod
    def _state(scenario: BookingScenario) -> dict[str, object]:
        def thaw(value):
            if isinstance(value, Mapping):
                return {key: thaw(item) for key, item in value.items()}
            if isinstance(value, tuple):
                return [thaw(item) for item in value]
            return value

        return thaw(scenario.state)

    @staticmethod
    def _callback(scenario: BookingScenario, action: str, index: int) -> str:
        return f"booking:v1:{scenario.id.hex}:{action}:{index}"

    @staticmethod
    def _parse_callback(raw: object) -> tuple[UUID, str, int] | None:
        if not isinstance(raw, str):
            return None
        parts = raw.split(":")
        if len(parts) != 5 or parts[:2] != ["booking", "v1"]:
            return None
        try:
            return UUID(hex=parts[2]), parts[3], int(parts[4])
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _inline_options(rows):
        return {
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": text, "callback_data": callback}
                        for text, callback in row
                    ]
                    for row in rows
                ]
            }
        }
