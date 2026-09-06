from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg

from moroz.booking.catalog import CatalogRepository, CatalogService
from moroz.booking.conversation import filter_slots, merge_draft, next_requirement
from moroz.booking.models import BookingIdentity, BookingScenario, Slot, SlotQuery
from moroz.booking.ports import BookingPort
from moroz.booking.repository import BookingRepository
from moroz.booking.service import BookingService
from moroz.booking.time_display import MOSCOW, format_booking_time
from moroz.booking.yclients_catalog import walk_in_family
from moroz.messaging.router import RouteDecision, bound_routing_state, valid_route_action
from moroz.messaging.telegram import remove_keyboard_options


STALE_REPLY = "Эта кнопка уже неактуальна. Напишите, пожалуйста, что хотите сделать."
CLARIFY_REPLY = "Уточните, пожалуйста, что хотите узнать или на какую услугу записаться."
_CALLBACK_ACTIONS = (
    "service",
    "slot",
    "booking",
    "booking_action",
    "confirm",
    "confirm_change",
)
_MAX_CHOICES = 3
_ANY_STAFF = {
    "без разницы",
    "все равно",
    "кто угодно",
    "любая",
    "любой",
    "любой специалист",
    "не важно",
    "неважно",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class BookingReply:
    text: str
    delivery_options: dict[str, object]


def normalize_russian_phone(value: str) -> str | None:
    if any(character not in "+()- .\t\r\n0123456789" for character in value):
        return None
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) == 10:
        digits = f"7{digits}"
    elif len(digits) == 11 and digits.startswith("8"):
        digits = f"7{digits[1:]}"
    if len(digits) != 11 or not digits.startswith("7"):
        return None
    return f"+{digits}"


def _normalise(value: object) -> str:
    return " ".join(
        str(value or "").strip().casefold().replace("ё", "е").replace("|", " ").split()
    )


def _is_any_staff(value: object) -> bool:
    normalized = _normalise(value)
    padded = f" {normalized} "
    return normalized in _ANY_STAFF or any(
        f" {phrase} " in padded for phrase in _ANY_STAFF
    )


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

    async def callback_origin(
        self,
        connection: asyncpg.Connection,
        customer_id: str,
        raw_callback: object,
    ) -> Mapping[str, object] | None:
        parsed = self._parse_callback(raw_callback)
        if parsed is None:
            return None
        row = await connection.fetchrow(
            "SELECT * FROM booking_scenarios WHERE id=$1 AND customer_id=$2 "
            "AND phase IN ('collecting','awaiting_confirmation')",
            parsed[0],
            customer_id,
        )
        if row is None:
            return None
        scenario = self._repository._scenario_from_row(row)
        if parsed[3] != self._callback_revision(scenario):
            return None
        origin = scenario.state.get("origin_update_id")
        if not origin:
            return None
        payload = await connection.fetchval(
            "SELECT payload FROM message_inbox WHERE channel='telegram' "
            "AND chat_id=$1 AND external_message_id=$2",
            customer_id,
            origin,
        )
        return json.loads(payload) if isinstance(payload, str) else payload

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
        decision: RouteDecision | None = None,
        origin_update_id: str | None = None,
    ) -> BookingReply | None:
        if kind == "callback":
            return await self._handle_callback(
                connection, customer_id, user_id, update_id, data.get("callback_data")
            )
        scenario = await self._repository.get_active_for_customer(customer_id)
        if kind == "contact":
            if scenario is None or scenario.kind != "create":
                return BookingReply(STALE_REPLY, remove_keyboard_options())
            return await self._collect_contact(connection, scenario, user_id, text, kind, data)
        if scenario is not None and next_requirement(scenario.state) == "contact":
            if normalize_russian_phone(text) is not None:
                return await self._collect_contact(
                    connection, scenario, user_id, text, kind, data
                )
        if decision is None:
            return None
        if not valid_route_action(decision):
            return BookingReply(CLARIFY_REPLY, {})
        if decision.route not in {"booking", "booking_management"}:
            return None
        if scenario is not None and scenario.phase == "executing":
            return BookingReply("Запись уже обрабатывается. Дождитесь результата.", {})
        if decision.action == "cancel_draft":
            return await self._cancel_draft(scenario)
        management = decision.route == "booking_management"
        if decision.action == "continue":
            if scenario is None:
                return BookingReply(CLARIFY_REPLY, {})
            management = (
                scenario.kind in {"reschedule", "cancel"}
                or scenario.state.get("mode") == "management"
            )
        if management:
            return await self._handle_management(
                connection,
                customer_id,
                update_id,
                decision,
                scenario,
                origin_update_id,
            )
        if decision.action not in {
            "create",
            "continue",
            "clarify",
            "none",
        }:
            return BookingReply(CLARIFY_REPLY, {})
        if scenario is None or scenario.kind != "create":
            if scenario is not None:
                await self._close_draft(scenario, "booking_flow_switched")
            scenario = await self._new_create_scenario(
                customer_id, update_id, origin_update_id
            )
        elif scenario.phase == "awaiting_confirmation":
            scenario = replace(scenario, phase="collecting", updated_at=self._now())
        state = merge_draft(self._state(scenario), decision)
        if (
            decision.staff is not None
            and state.get("service_id")
            and not self._resolve_staff_preference(state)
        ):
            scenario = await self._checkpoint(
                scenario, state, "booking_details_merged"
            )
            return await self._save_step(
                scenario,
                "staff",
                "Уточните имя специалиста или напишите «любой специалист».",
            )
        if (
            decision.action == "clarify"
            and not decision.time_from
            and not decision.time_to
            and len(decision.services) <= 1
        ):
            state["time_needs_clarification"] = True
        scenario = await self._checkpoint(scenario, state, "booking_details_merged")
        if decision.choice is not None:
            return await self._apply_choice(
                connection, scenario, user_id, update_id, decision.choice
            )
        if next_requirement(scenario.state) == "name" and text.strip():
            state = self._state(scenario)
            state["customer_name"] = text.strip()[:160]
            return await self._show_confirmation(scenario, state)
        return await self._advance(connection, scenario)

    async def routing_context(self, customer_id: str) -> str:
        scenario = await self._repository.get_active_for_customer(customer_id)
        state: dict[str, object] = {
            "today": self._now().astimezone(MOSCOW).date().isoformat(),
            "active": scenario is not None,
            "mode": "idle" if scenario is None else "booking",
        }
        if scenario is not None:
            choices = scenario.state.get("choices", ())
            state.update(
                {
                    "kind": scenario.kind,
                    "step": str(scenario.state.get("step", "")),
                    "service": scenario.state.get("service_name")
                    or scenario.state.get("service_query"),
                    "date": scenario.state.get("date"),
                    "time_from": scenario.state.get("time_from"),
                    "time_to": scenario.state.get("time_to"),
                    "staff": scenario.state.get("staff_name")
                    or scenario.state.get("staff_query"),
                    "choices": [
                        {"index": index, "label": str(item.get("label", ""))[:128]}
                        for index, item in enumerate(choices)
                        if isinstance(item, Mapping)
                    ][:_MAX_CHOICES],
                }
            )
            if scenario.state.get("mode") == "management":
                state["mode"] = "booking_management"
        return bound_routing_state(json.dumps(state, ensure_ascii=False)) or "{}"

    async def _new_create_scenario(
        self, customer_id: str, update_id: str, origin_update_id: str | None
    ) -> BookingScenario:
        scenario = BookingScenario(
            uuid4(),
            "create",
            "collecting",
            f"telegram:create:{update_id}",
            customer_id,
            {"source": "telegram", "origin_update_id": origin_update_id or update_id},
            None,
            self._now(),
            self._now(),
        )
        try:
            scenario_id = await self._repository.create_scenario(scenario)
        except asyncpg.UniqueViolationError:
            active = await self._repository.get_active_for_customer(customer_id)
            if active is None:
                raise
            return active
        stored = await self._repository.get_scenario(scenario_id)
        if stored is None:
            raise RuntimeError("created booking scenario is missing")
        return stored

    async def _advance(
        self, connection: asyncpg.Connection, scenario: BookingScenario
    ) -> BookingReply:
        requirement = next_requirement(scenario.state)
        if requirement == "service":
            return await self._resolve_service(connection, scenario)
        if requirement == "date":
            return await self._save_step(scenario, "date", "На какую дату хотите записаться?")
        if requirement == "time":
            return await self._save_step(
                scenario,
                "time",
                "Уточните время цифрами, например: после 18:00 или с 12:00 до 15:00.",
            )
        if requirement == "slot":
            return await self._offer_slots(scenario)
        if requirement == "contact":
            return await self._request_contact(scenario)
        if requirement == "name":
            return await self._save_step(scenario, "name", "Как вас зовут?")
        return await self._show_confirmation(scenario, self._state(scenario))

    async def _resolve_service(
        self, connection: asyncpg.Connection, scenario: BookingScenario
    ) -> BookingReply:
        services = await self._catalog.list_services(connection, self._now())
        if not services:
            return BookingReply(
                "Сейчас не могу подтвердить актуальные услуги. Напишите администратору.", {}
            )
        state = self._state(scenario)
        raw_candidates = state.get("service_candidates")
        queries = (
            [str(value) for value in raw_candidates]
            if isinstance(raw_candidates, list)
            else [str(state.get("service_query", ""))]
        )
        matches: list[CatalogService] = []
        for query in queries:
            for service in self._matching_services(services, query):
                if all(item.service_id != service.service_id for item in matches):
                    matches.append(service)
        matches = matches[:_MAX_CHOICES]
        if not matches:
            return await self._save_step(
                scenario,
                "service",
                "Не нашёл такую услугу в актуальном каталоге. Уточните название.",
            )
        if len(matches) > 1:
            state.update(
                {"step": "service", "choices": [self._service_choice(item) for item in matches]}
            )
            updated = await self._checkpoint(scenario, state, "booking_service_choices")
            return self._choice_reply(
                updated, "Какую одну услугу оформить сейчас?", "service"
            )
        return await self._select_service(connection, scenario, matches[0])

    async def _select_service(
        self,
        connection: asyncpg.Connection,
        scenario: BookingScenario,
        service: CatalogService,
    ) -> BookingReply:
        if walk_in_family(service.service_name) is not None:
            await self._close_draft(scenario, "booking_walk_in_selected")
            return BookingReply(
                f"{service.service_name}: предварительная запись не нужна. "
                "Можно прийти ежедневно с 10:00 до 21:00.",
                {},
            )
        state = self._state(scenario)
        state.update(
            {
                "service_id": service.service_id,
                "service_name": service.service_name,
                "staff_names": {
                    item.staff_id: item.staff_name for item in service.variants
                },
            }
        )
        state.pop("service_candidates", None)
        state.pop("choices", None)
        staff_query = _normalise(state.get("staff_query"))
        if staff_query and not _is_any_staff(staff_query):
            variants = [
                item
                for item in service.variants
                if staff_query in _normalise(item.staff_name)
                or _normalise(item.staff_name) in staff_query
            ]
            if len(variants) != 1:
                updated = await self._checkpoint(
                    scenario, state, "booking_service_selected"
                )
                return await self._save_step(
                    updated,
                    "staff",
                    "Уточните имя специалиста или напишите «любой специалист».",
                )
            state["staff_id"] = variants[0].staff_id
            state["staff_name"] = variants[0].staff_name
        else:
            state["staff_id"] = None
            state["staff_name"] = "Любой специалист"
        updated = await self._checkpoint(scenario, state, "booking_service_selected")
        return await self._advance(connection, updated)

    async def _offer_slots(self, scenario: BookingScenario) -> BookingReply:
        state = self._state(scenario)
        day = datetime.fromisoformat(str(state["date"])).replace(tzinfo=MOSCOW)
        end = day + timedelta(days=1)
        now = self._now()
        if end <= now:
            return await self._save_step(
                scenario, "date", "Эта дата уже прошла. Укажите будущую дату."
            )
        query = SlotQuery(
            (str(state["service_id"]),),
            max(now, day),
            end,
            str(state["staff_id"]) if state.get("staff_id") else None,
        )
        slots = filter_slots(
            sorted(await self._port.list_slots(query), key=lambda item: item.starts_at),
            str(state["time_from"]) if state.get("time_from") else None,
            str(state["time_to"]) if state.get("time_to") else None,
        )
        if not slots:
            return BookingReply(
                f"На эту дату{self._window_text(state)} свободного времени не нашлось. "
                "Назовите другую дату или время.",
                {},
            )
        choices = [self._slot_choice(slot) for slot in slots[:_MAX_CHOICES]]
        state.update(
            {
                "step": "slot",
                "slot_query": {
                    "service_ids": [str(state["service_id"])],
                    "starts_after": query.starts_after.isoformat(),
                    "starts_before": query.starts_before.isoformat(),
                    "staff_id": query.staff_id,
                },
                "available_slots": choices,
                "choices": choices,
            }
        )
        updated = await self._checkpoint(scenario, state, "booking_slots_offered")
        return self._choice_reply(updated, self._slot_header(updated), "slot")

    async def _request_contact(self, scenario: BookingScenario) -> BookingReply:
        state = self._state(scenario)
        state["step"] = "contact"
        await self._checkpoint(scenario, state, "booking_contact_requested")
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
        if next_requirement(scenario.state) != "contact":
            return BookingReply(STALE_REPLY, {})
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
                value.strip()
                for value in (
                    str(data.get("first_name", "")),
                    str(data.get("last_name", "")),
                )
                if value.strip()
            )
        else:
            raw_phone = text
        phone = normalize_russian_phone(raw_phone)
        if phone is None:
            return BookingReply("Проверьте номер телефона и отправьте его ещё раз.", {})
        state = self._state(scenario)
        state.update(
            {
                "customer_phone": phone,
                "processing_consent": True,
                "personal_data_processing_allowed": True,
            }
        )
        if name:
            state["customer_name"] = name[:160]
            return await self._show_confirmation(scenario, state)
        updated = await self._checkpoint(scenario, state, "booking_phone_collected")
        return await self._save_step(updated, "name", "Как вас зовут?")

    async def _show_confirmation(
        self, scenario: BookingScenario, state: dict[str, object]
    ) -> BookingReply:
        state["step"] = "confirm"
        updated = replace(
            scenario,
            phase="awaiting_confirmation",
            state=state,
            updated_at=self._now(),
        )
        await self._repository.checkpoint(updated, "booking_details_collected")
        phone = str(state["customer_phone"])
        masked = f"{phone[:2]}******{phone[-4:]}"
        return BookingReply(
            "Проверьте запись:\n"
            f"{state['service_name']}\n{state['staff_name']}\n"
            f"{format_booking_time(str(state['starts_at']))}\n"
            f"{state['customer_name']}, {masked}",
            self._inline_options(
                [[("Подтвердить", self._callback(updated, "confirm", 0))]]
            ),
        )

    async def _handle_management(
        self,
        connection: asyncpg.Connection,
        customer_id: str,
        update_id: str,
        decision: RouteDecision,
        active: BookingScenario | None,
        origin_update_id: str | None,
    ) -> BookingReply:
        if active is not None and active.kind == "reschedule":
            if decision.action not in {"none", "continue", "clarify", "reschedule"}:
                return BookingReply(CLARIFY_REPLY, {})
            state = merge_draft(self._state(active), decision)
            # For a reschedule this is the original booking time, not the new slot.
            state["starts_at"] = active.state["starts_at"]
            if active.phase == "awaiting_confirmation":
                if state.get("selected_slot_id"):
                    return self._render_current(active)
                active = replace(active, phase="collecting", updated_at=self._now())
            if not self._resolve_staff_preference(state):
                updated = await self._checkpoint(
                    active, state, "booking_reschedule_details_merged"
                )
                return await self._save_step(
                    updated,
                    "staff",
                    "Уточните имя специалиста или напишите «любой специалист».",
                )
            updated = await self._checkpoint(
                active, state, "booking_reschedule_details_merged"
            )
            if decision.choice is not None:
                return await self._apply_choice(
                    connection, updated, customer_id, update_id, decision.choice
                )
            return await self._advance(connection, updated)
        if active is not None and active.kind == "cancel":
            return self._render_current(active)
        if active is not None and active.state.get("mode") == "management":
            state = self._state(active)
            if decision.action in {"cancel", "reschedule"}:
                state["management_operation"] = decision.action
            if decision.date is not None:
                state["requested_date"] = decision.date
            if decision.time_from is not None or decision.time_to is not None:
                state["time_from"] = decision.time_from
                state["time_to"] = decision.time_to
            if decision.staff is not None:
                state["staff_query"] = decision.staff
            if state != active.state:
                active = await self._checkpoint(
                    active, state, "booking_management_details_merged"
                )
            if decision.choice is not None:
                return await self._apply_choice(
                    connection, active, customer_id, update_id, decision.choice
                )
            if (
                active.state.get("step") == "booking_action"
                and decision.action in {"cancel", "reschedule"}
            ):
                return await self._begin_change(active, decision.action)
            return self._render_current(active)
        if active is not None:
            await self._close_draft(active, "booking_flow_switched")
        owned = await self._repository.list_future_owned(customer_id, self._now())
        if not owned:
            return BookingReply(
                "Здесь можно управлять только будущими записями, созданными "
                "через этот Telegram-чат. По остальным поможет администратор.",
                {},
            )
        choices = []
        for booking, raw_state in owned[:_MAX_CHOICES]:
            state = self._state_item(raw_state)
            service_name = str(state.get("service_name", "Услуга"))
            choices.append(
                {
                    "external_id": booking.external_id,
                    "booking_key": str(booking.booking_key),
                    "slot_id": booking.slot_id,
                    "starts_at": booking.starts_at.isoformat(),
                    "service_id": str(state.get("service_id", "")),
                    "service_name": service_name,
                    "staff_id": state.get("selected_staff_id"),
                    "staff_name": str(state.get("staff_name", "Любой специалист")),
                    "staff_names": self._state_item(state.get("staff_names", {})),
                    "label": f"{format_booking_time(booking.starts_at)} — {service_name}",
                }
            )
        scenario = BookingScenario(
            uuid4(),
            "create",
            "collecting",
            f"telegram:manage:{update_id}",
            customer_id,
            {
                "source": "telegram",
                "origin_update_id": origin_update_id or update_id,
                "mode": "management",
                "step": "booking",
                "management_operation": decision.action,
                "requested_date": decision.date,
                "time_from": decision.time_from,
                "time_to": decision.time_to,
                "staff_query": decision.staff,
                "choices": choices,
            },
            None,
            self._now(),
            self._now(),
        )
        scenario_id = await self._repository.create_scenario(scenario)
        stored = await self._repository.get_scenario(scenario_id)
        if stored is None:
            raise RuntimeError("created management scenario is missing")
        if len(choices) == 1:
            return await self._choose_owned_booking(stored, choices[0])
        return self._choice_reply(stored, "Выберите свою запись", "booking")

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
        operation = state.get("management_operation")
        if operation in {"cancel", "reschedule"}:
            return await self._begin_change(updated, str(operation))
        return self._choice_reply(
            updated, f"Запись: {choice['label']}\nЧто сделать?", "booking_action"
        )

    async def _begin_change(
        self, management: BookingScenario, operation: str
    ) -> BookingReply:
        if operation not in {"reschedule", "cancel"}:
            return BookingReply(STALE_REPLY, {})
        selected = management.state.get("selected_booking")
        if not isinstance(selected, Mapping):
            return BookingReply(STALE_REPLY, {})
        await self._close_draft(management, "booking_management_completed")
        base_state = {
            "source": "telegram",
            "origin_update_id": management.state.get("origin_update_id"),
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
            uuid4(),
            operation,
            "collecting" if operation == "reschedule" else "awaiting_confirmation",
            f"telegram:{operation}:{management.id.hex}",
            management.customer_id,
            base_state,
            None,
            self._now(),
            self._now(),
        )
        if operation == "cancel":
            scenario = replace(scenario, state={**base_state, "step": "confirm_change"})
            await self._repository.create_scenario(scenario)
            return BookingReply(
                f"Отменить запись на {format_booking_time(str(selected['starts_at']))}?",
                self._inline_options(
                    [[("Да, отменить", self._callback(scenario, "confirm_change", 0))]]
                ),
            )
        state = {
            **base_state,
            "staff_id": None,
            "staff_name": "Любой специалист",
            "date": management.state.get("requested_date"),
            "time_from": management.state.get("time_from"),
            "time_to": management.state.get("time_to"),
            "staff_query": management.state.get("staff_query"),
        }
        if not self._resolve_staff_preference(state):
            scenario = replace(scenario, state=state)
            await self._repository.create_scenario(scenario)
            stored = await self._repository.get_scenario(scenario.id)
            if stored is None:
                raise RuntimeError("created reschedule scenario is missing")
            return await self._save_step(
                stored,
                "staff",
                "Уточните имя специалиста или напишите «любой специалист».",
            )
        scenario = replace(scenario, state=state)
        await self._repository.create_scenario(scenario)
        stored = await self._repository.get_scenario(scenario.id)
        if stored is None:
            raise RuntimeError("created reschedule scenario is missing")
        if not stored.state.get("date"):
            return await self._save_step(
                stored, "date", "На какую новую дату перенести запись?"
            )
        return await self._offer_slots(stored)

    @staticmethod
    def _resolve_staff_preference(state: dict[str, object]) -> bool:
        query = _normalise(state.get("staff_query"))
        if not query or _is_any_staff(query):
            state["staff_id"] = None
            state["staff_name"] = "Любой специалист"
            return True
        staff_names = state.get("staff_names")
        if not isinstance(staff_names, Mapping):
            return False
        matches = [
            (str(staff_id), str(staff_name))
            for staff_id, staff_name in staff_names.items()
            if query in _normalise(staff_name) or _normalise(staff_name) in query
        ]
        if len(matches) != 1:
            return False
        state["staff_id"], state["staff_name"] = matches[0]
        return True

    async def _handle_callback(
        self,
        connection: asyncpg.Connection,
        customer_id: str,
        user_id: str,
        update_id: str,
        raw_callback: object,
    ) -> BookingReply:
        parsed = self._parse_callback(raw_callback)
        if parsed is None:
            return await self._recover_callback(customer_id)
        scenario_id, action, index, revision = parsed
        scenario = await self._repository.get_scenario(scenario_id)
        if scenario is None or scenario.customer_id != customer_id:
            return await self._recover_callback(customer_id)
        if (
            scenario.phase == "confirmed"
            and action in {"confirm", "confirm_change"}
            and scenario.state.get("confirmation_update_id") != update_id
        ):
            return BookingReply("", {})
        if revision != self._callback_revision(scenario):
            return await self._recover_callback(customer_id)
        if (
            action in {"confirm", "confirm_change"}
            and index == 0
            and scenario.phase in {"awaiting_confirmation", "confirmed"}
            and scenario.state.get("step") == action
        ):
            state = self._state(scenario)
            state["confirmation_update_id"] = update_id
            scenario = await self._checkpoint(
                scenario, state, "booking_confirmation_received"
            )
            result = await self._booking_service.handle(
                scenario.id,
                confirmed=True,
                identity=(
                    BookingIdentity(customer_id, confirmed=True)
                    if scenario.kind in {"reschedule", "cancel"}
                    else None
                ),
            )
            if result.next_action == "choose_slot":
                current = await self._repository.get_scenario(scenario.id)
                if current is not None:
                    return await self._offer_slots(current)
            return BookingReply(result.message, {})
        if scenario.phase != "collecting" or scenario.state.get("step") != action:
            return await self._recover_callback(customer_id)
        return await self._apply_choice(
            connection, scenario, user_id, update_id, index
        )

    async def _apply_choice(
        self,
        connection: asyncpg.Connection | None,
        scenario: BookingScenario,
        user_id: str,
        update_id: str,
        index: int,
    ) -> BookingReply:
        del user_id, update_id
        choices = scenario.state.get("choices")
        if not isinstance(choices, tuple) or not 0 <= index < len(choices):
            return await self._recover_callback(scenario.customer_id)
        choice = choices[index]
        if not isinstance(choice, Mapping):
            return await self._recover_callback(scenario.customer_id)
        step = str(scenario.state.get("step", ""))
        if step == "service" and connection is not None:
            services = await self._catalog.list_services(connection, self._now())
            selected = next(
                (
                    item
                    for item in services
                    if item.service_id == str(choice.get("service_id"))
                ),
                None,
            )
            if selected is None:
                return BookingReply(
                    "Этой услуги больше нет в актуальном каталоге. Уточните название.", {}
                )
            return await self._select_service(connection, scenario, selected)
        if step == "slot":
            return await self._choose_slot(scenario, choice)
        if step == "booking":
            return await self._choose_owned_booking(scenario, choice)
        if step == "booking_action":
            return await self._begin_change(scenario, str(choice.get("operation", "")))
        return await self._recover_callback(scenario.customer_id)

    async def _choose_slot(
        self, scenario: BookingScenario, choice: Mapping[str, object]
    ) -> BookingReply:
        state = self._state(scenario)
        state.update(
            {
                "selected_slot_id": str(choice["slot_id"]),
                "selected_staff_id": choice.get("staff_id"),
            }
        )
        state.pop("choices", None)
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
                f"Перенести запись на {format_booking_time(str(choice['starts_at']))}?",
                self._inline_options(
                    [[("Да, перенести", self._callback(updated, "confirm_change", 0))]]
                ),
            )
        state["starts_at"] = str(choice["starts_at"])
        updated = await self._checkpoint(scenario, state, "booking_slot_selected")
        requirement = next_requirement(updated.state)
        if requirement == "contact":
            return await self._request_contact(updated)
        if requirement == "name":
            return await self._save_step(updated, "name", "Как вас зовут?")
        return await self._show_confirmation(updated, self._state(updated))

    async def _save_step(
        self, scenario: BookingScenario, step: str, text: str
    ) -> BookingReply:
        state = self._state(scenario)
        state["step"] = step
        state.pop("choices", None)
        await self._checkpoint(scenario, state, f"booking_{step}_requested")
        return BookingReply(text, {})

    async def _cancel_draft(self, scenario: BookingScenario | None) -> BookingReply:
        if scenario is None:
            return BookingReply("Сейчас нет незавершённого оформления.", {})
        await self._close_draft(scenario, "booking_flow_cancelled", "user_cancelled")
        return BookingReply("Текущее действие отменено.", {})

    async def _close_draft(
        self,
        scenario: BookingScenario,
        event: str,
        error_code: str | None = None,
    ) -> None:
        await self._repository.checkpoint(
            replace(
                scenario,
                phase="failed",
                error_code=error_code,
                updated_at=self._now(),
            ),
            event,
        )

    async def _checkpoint(
        self, scenario: BookingScenario, state: Mapping[str, object], event: str
    ) -> BookingScenario:
        updated = replace(scenario, state=dict(state), updated_at=self._now())
        await self._repository.checkpoint(updated, event)
        return updated

    async def _recover_callback(self, customer_id: str) -> BookingReply:
        active = await self._repository.get_active_for_customer(customer_id)
        if active is None or str(active.state.get("step", "")).startswith("catalog_"):
            return BookingReply(STALE_REPLY, remove_keyboard_options())
        current = self._render_current(active)
        return BookingReply(f"{STALE_REPLY}\n\n{current.text}", current.delivery_options)

    def _render_current(self, scenario: BookingScenario) -> BookingReply:
        step = str(scenario.state.get("step", ""))
        if step in {"service", "slot", "booking", "booking_action"}:
            action = step
            if step == "service":
                text = "Какую одну услугу оформить сейчас?"
            elif step == "slot":
                text = self._slot_header(scenario)
            elif step == "booking":
                text = "Выберите свою запись"
            else:
                text = "Что сделать с записью?"
            return self._choice_reply(scenario, text, action)
        if step == "contact":
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
        if step == "name":
            return BookingReply("Как вас зовут?", {})
        if step in {"confirm", "confirm_change"}:
            action = "confirm" if step == "confirm" else "confirm_change"
            label = {
                "create": "Подтвердить",
                "reschedule": "Да, перенести",
                "cancel": "Да, отменить",
            }[scenario.kind]
            return BookingReply(
                "Подтвердите действие кнопкой ниже.",
                self._inline_options(
                    [[(label, self._callback(scenario, action, 0))]]
                ),
            )
        prompts = {
            "date": "На какую дату хотите записаться?",
            "time": "Уточните точное время цифрами.",
            "staff": "Уточните имя специалиста или напишите «любой специалист».",
        }
        return BookingReply(prompts.get(step, CLARIFY_REPLY), {})

    def _choice_reply(
        self, scenario: BookingScenario, text: str, action: str
    ) -> BookingReply:
        choices = scenario.state.get("choices")
        values = choices if isinstance(choices, tuple) else ()
        rows = [
            [(str(choice["label"]), self._callback(scenario, action, index))]
            for index, choice in enumerate(values[:_MAX_CHOICES])
            if isinstance(choice, Mapping)
        ]
        return BookingReply(text, self._inline_options(rows) if rows else {})

    @staticmethod
    def _matching_services(
        services: tuple[CatalogService, ...], query: str
    ) -> list[CatalogService]:
        value = _normalise(query)
        exact = [item for item in services if _normalise(item.service_name) == value]
        if exact:
            return exact
        return [
            item
            for item in services
            if value
            and (
                value in _normalise(item.service_name)
                or _normalise(item.service_name) in value
            )
        ]

    @staticmethod
    def _service_choice(service: CatalogService) -> dict[str, object]:
        return {"service_id": service.service_id, "label": service.service_name}

    @staticmethod
    def _slot_choice(slot: Slot) -> dict[str, object]:
        return {
            "slot_id": slot.id,
            "starts_at": slot.starts_at.astimezone(MOSCOW).isoformat(),
            "staff_id": slot.staff_id,
            "label": slot.starts_at.astimezone(MOSCOW).strftime("%H:%M"),
        }

    @classmethod
    def _callback_revision(cls, scenario: BookingScenario) -> str:
        state = cls._state(scenario)
        view = {
            key: state.get(key)
            for key in (
                "step",
                "choices",
                "selected_slot_id",
                "new_starts_at",
                "selected_booking",
                "date",
                "time_from",
                "time_to",
            )
        }
        return hashlib.sha256(
            json.dumps(view, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:12]

    @classmethod
    def _callback(cls, scenario: BookingScenario, action: str, index: int) -> str:
        code = _CALLBACK_ACTIONS.index(action)
        return (
            f"booking:v1:{scenario.id.hex}:{code}:{index}."
            f"{cls._callback_revision(scenario)}"
        )

    @staticmethod
    def _parse_callback(
        raw: object,
    ) -> tuple[UUID, str, int, str | None] | None:
        if not isinstance(raw, str):
            return None
        parts = raw.split(":")
        if len(parts) != 5 or parts[:2] != ["booking", "v1"]:
            return None
        try:
            index, revision = parts[4].split(".", 1)
            code = int(parts[3])
            if not 0 <= code < len(_CALLBACK_ACTIONS):
                return None
            return UUID(hex=parts[2]), _CALLBACK_ACTIONS[code], int(index), revision
        except (ValueError, TypeError, IndexError):
            return None

    @staticmethod
    def _inline_options(rows: list[list[tuple[str, str]]]) -> dict[str, object]:
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

    @staticmethod
    def _state_item(value: object) -> dict[str, object]:
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _state(scenario: BookingScenario) -> dict[str, object]:
        def thaw(value: object) -> object:
            if isinstance(value, Mapping):
                return {key: thaw(item) for key, item in value.items()}
            if isinstance(value, tuple):
                return [thaw(item) for item in value]
            return value

        return thaw(scenario.state)  # type: ignore[return-value]

    @staticmethod
    def _window_text(state: Mapping[str, object]) -> str:
        if state.get("time_from") and state.get("time_to"):
            return f" с {state['time_from']} до {state['time_to']}"
        if state.get("time_from"):
            return f" после {state['time_from']}"
        if state.get("time_to"):
            return f" до {state['time_to']}"
        return ""

    @classmethod
    def _slot_header(cls, scenario: BookingScenario) -> str:
        state = scenario.state
        day = datetime.fromisoformat(str(state["date"])).strftime("%d.%m.%Y")
        return (
            "Выберите время\n"
            f"{state['service_name']}\n{day}{cls._window_text(state)} · московское время"
        )
