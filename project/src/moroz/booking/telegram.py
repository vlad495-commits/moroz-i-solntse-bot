from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
import json
import hashlib
import re

import asyncpg

from moroz.booking.catalog import (
    CatalogGrounding,
    CatalogRepository,
    CatalogService,
    _range_text,
    service_duration_is_meaningful,
)
from moroz.booking.models import BookingIdentity, BookingScenario, Slot, SlotQuery
from moroz.booking.ports import BookingPort
from moroz.booking.repository import BookingRepository
from moroz.booking.service import BookingService
from moroz.booking.yclients_catalog import walk_in_family
from moroz.messaging.telegram import main_menu_options
from moroz.messaging.router import RouteDecision, bound_routing_state, valid_route_action
from moroz.booking.time_display import MOSCOW, format_booking_time
from moroz.booking.display import service_display_name, procedure_description, procedure_details, asks_component_duration


STALE_REPLY = "Эта кнопка уже неактуальна. Начните запись заново."
OUTDATED_BUTTON_REPLY = "Эта кнопка уже неактуальна. Показываю текущий шаг."
CLARIFY_REPLY = "Уточните, пожалуйста: хотите узнать об услуге, посмотреть свободное время или свои записи?"
_MENU_BOOK = "📅 Записаться"
_MENU_CATALOG = "✨ Услуги и цены"
_MENU_MY_BOOKINGS = "📋 Мои записи"
_MENU_LABELS = frozenset(
    {
        _MENU_BOOK,
        _MENU_CATALOG,
        "📍 Адрес и режим",
        "👩‍💼 Позвать администратора",
        "👩‍💼 Связаться с администратором",
        "🧭 Подобрать процедуру",
        _MENU_MY_BOOKINGS,
    }
)
_WALK_IN_LABELS = {
    "collagenarium": "Коллагенарий",
    "collarium": "Коллариум",
    "solarium": "Солярий",
}
_CALLBACK_ACTIONS = ("service", "staff", "available_date", "slot", "booking_management", "booking_action", "confirm", "confirm_change", "page", "catalog_category", "catalog_service", "catalog_book", "cancel_draft", "clear_time_after", "catalog_root", "choose_date", "catalog_back", "booking_back")
_DRAFT_CANCEL_COMMANDS = frozenset({"отменить действие", "выйти из оформления"})
_TIME_AFTER = re.compile(r"(?:после|не раньше)\s*(\d{1,2})(?:[:.](\d{2}))?", re.IGNORECASE)


def persistent_menu_command(text: str) -> str | None:
    command = text.strip()
    return command if command in _MENU_LABELS else None


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

    async def callback_origin(self, connection, customer_id: str, raw_callback: object):
        """A current button on a new draft proves it was issued after its origin.

        This is the timestamp substitute for Telegram callbacks after an ID reset.
        """
        parsed = self._parse_callback(raw_callback)
        if parsed is None:
            return None
        row = await connection.fetchrow(
            "SELECT * FROM booking_scenarios WHERE id=$1 AND customer_id=$2 "
            "AND phase IN ('collecting','awaiting_confirmation')",
            parsed[0], customer_id,
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
            "SELECT payload FROM message_inbox WHERE channel='telegram' AND chat_id=$1 AND external_message_id=$2",
            customer_id, origin,
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
                connection,
                customer_id,
                user_id,
                update_id,
                data.get("callback_data"),
            )

        scenario = await self._repository.get_active_for_customer(customer_id)
        menu_command = persistent_menu_command(text) if kind == "text" else None
        requested_time_after = self._requested_time_after(text) if kind == "text" else None
        if kind == "text" and text.strip().casefold() in _DRAFT_CANCEL_COMMANDS:
            return await self._cancel_draft(scenario)
        if (
            requested_time_after is not None
            and decision is not None
            and valid_route_action(decision)
            and decision.route == "booking"
            and decision.action in {"create", "continue"}
            and decision.service is None
            and decision.date is None
            and decision.choice is None
            and scenario is not None
            and scenario.phase == "collecting"
            and not str(scenario.state.get("step", "")).startswith("catalog_")
        ):
            state = self._state(scenario)
            state["requested_time_after"] = requested_time_after
            scenario = await self._checkpoint(
                scenario, state, "booking_time_limit_requested"
            )
            if state.get("step") == "slot" and state.get("selected_date"):
                return await self._choose_date(
                    scenario, {"date": state["selected_date"]}
                )
        if (
            scenario is not None
            and scenario.phase in {"collecting", "awaiting_confirmation"}
            and scenario.idempotency_key == f"telegram:create:{update_id}"
            and (
                menu_command == _MENU_BOOK
                or (menu_command is None and decision is not None
                    and valid_route_action(decision)
                    and decision.route == "booking" and decision.action == "create")
            )
        ):
            # Coordinator checkpoints survive a rolled-back worker inbox transaction.
            return await self._refresh_current(connection, scenario)
        if menu_command is not None:
            if menu_command == _MENU_CATALOG and scenario is not None and scenario.idempotency_key == f"telegram:catalog:{update_id}":
                return await self._refresh_current(connection, scenario)
            if (
                menu_command == _MENU_MY_BOOKINGS
                and scenario is not None
                and scenario.idempotency_key == f"telegram:manage:{update_id}"
            ):
                return await self._refresh_current(connection, scenario)
            if scenario is not None:
                if scenario.phase == "executing":
                    return BookingReply(
                        "Запись уже обрабатывается. Дождитесь результата.",
                        {},
                    )
                await self._repository.checkpoint(
                    replace(
                        scenario,
                        phase="failed",
                        error_code="menu_navigation",
                        updated_at=self._now(),
                    ),
                    "booking_flow_left_for_menu",
                )
            if menu_command == _MENU_BOOK:
                return await self._start(connection, customer_id, update_id, origin_update_id=origin_update_id)
            if menu_command == _MENU_CATALOG:
                return await self._start_catalog(connection, customer_id, update_id, origin_update_id=origin_update_id)
            if menu_command == _MENU_MY_BOOKINGS:
                return await self._start_management(
                    customer_id,
                    update_id,
                    operation="view",
                    origin_update_id=origin_update_id,
                )
            if menu_command == "🧭 Подобрать процедуру":
                return BookingReply(
                    "Что вам сейчас ближе? Выберите цель или опишите пожелания своими словами.",
                    {"reply_markup": {"keyboard": [
                        [{"text": "Отдых"}, {"text": "Загар"}],
                        [{"text": "Уход"}, {"text": "Массаж"}],
                        [{"text": _MENU_CATALOG}, {"text": _MENU_BOOK}],
                    ], "resize_keyboard": True}},
                )
            return None
        if kind == "text":
            question = text.strip().casefold().rstrip("?!. ")
            if question in {"подарок", "сертификат", "подарочный сертификат", "хочу подарочный сертификат", "хочу сделать подарок"}:
                return BookingReply(
                    "Подарочный сертификат можно оформить на сумму, программу или процедуру. "
                    "Выберите оформление онлайн или уточните вариант у администратора.",
                    {"reply_markup": {"inline_keyboard": [
                        [{"text": "Оформить сертификат", "url": "https://n1321481.yclients.com"}],
                        [{"text": "Уточнить у администратора", "url": "https://t.me/krio_71"}],
                    ]}},
                )
            if scenario is not None and scenario.state.get("step") == "catalog_book":
                short = question in {"что это", "что это такое", "расскажи подробнее", "подробнее", "кому подходит", "есть противопоказания", "какие противопоказания", "а сколько длится", "сколько длится"}
                parts = asks_component_duration(question)
                if short or parts:
                    services = await self._catalog.list_services(connection, self._now())
                    service = next((s for s in services if s.service_id == scenario.state.get("catalog_service_id")), None)
                    if service is not None:
                        if parts:
                            return BookingReply(procedure_description(service.service_name) + "\n\nРазбивка времени по отдельным процедурам не подтверждена. Её уточнит администратор.", main_menu_options())
                        if "длится" in question:
                            return BookingReply(CatalogGrounding("fresh", (service,), "duration", False).direct_reply, main_menu_options())
                        return BookingReply(procedure_details(service.service_name), main_menu_options())
        if decision is None:
            if scenario is not None and scenario.state.get("step") == "contact" and (
                kind == "contact" or normalize_russian_phone(text) is not None
            ):
                return await self._collect_contact(connection, scenario, user_id, text, kind, data)
            return BookingReply(STALE_REPLY, main_menu_options()) if kind == "contact" else None
        if not valid_route_action(decision) or decision.action == 'clarify':
            return BookingReply(CLARIFY_REPLY, {})
        if decision.action == 'clarify_cancel':
            return BookingReply("Вы хотите прекратить текущее оформление или отменить уже созданную запись?", {})
        if decision.route not in {"booking", "booking_management"}:
            return None
        if scenario is not None and scenario.phase == "executing":
            return BookingReply("Запись уже обрабатывается. Дождитесь результата.", {})
        if decision.action == "cancel_draft":
            return await self._cancel_draft(scenario)

        step = str(scenario.state.get('step', '')) if scenario is not None else ''
        browsing = step.startswith('catalog_')
        if decision.action == 'none':
            return await self._refresh_current(connection, scenario) if scenario else BookingReply(CLARIFY_REPLY, {})
        if decision.action == 'continue' and (scenario is None or browsing):
            return await self._refresh_current(connection, scenario) if scenario else BookingReply(CLARIFY_REPLY, {})
        if decision.action == 'provide_name' and step != 'name':
            return BookingReply(CLARIFY_REPLY, {})
        if decision.choice is not None:
            choices = scenario.state.get('choices', ()) if scenario is not None else ()
            page = max(0, int(scenario.state.get('page', 0))) if scenario is not None else 0
            if not page * 8 <= decision.choice < min(len(choices), (page + 1) * 8):
                return BookingReply(CLARIFY_REPLY, {})

        if decision.route == "booking_management":
            if decision.action == 'continue' and step not in {'booking_management', 'booking_action'}:
                return BookingReply(CLARIFY_REPLY, {})
            if scenario is not None and scenario.state.get("step") in {"booking_management", "booking_action"}:
                if scenario.state.get("step") == "booking_action" and decision.action in {"cancel", "reschedule"}:
                    return await self._begin_change(scenario, {"operation": decision.action})
                if decision.choice is not None:
                    return await self._semantic_choice(connection, scenario, customer_id, user_id, update_id, decision.choice)
                return self._render_current(scenario)
            if scenario is not None:
                await self._repository.checkpoint(replace(scenario, phase="failed", updated_at=self._now()), "booking_flow_switched")
            return await self._start_management(customer_id, update_id, operation=decision.action, origin_update_id=origin_update_id)
        if scenario is None or browsing:
            if decision.action != 'create':
                return BookingReply(CLARIFY_REPLY, {})
            if scenario is not None:
                await self._repository.checkpoint(replace(scenario, phase='failed', updated_at=self._now()), 'booking_flow_switched')
            return await self._start(
                connection,
                customer_id,
                update_id,
                decision=decision,
                origin_update_id=origin_update_id,
                requested_time_after=requested_time_after,
            )
        if step not in {'service', 'staff', 'available_date', 'slot', 'contact', 'name', 'confirm', 'confirm_change', 'booking_management', 'booking_action'}:
            return BookingReply(CLARIFY_REPLY, {})
        if decision.service and decision.action == 'create':
            decision = replace(decision, date=decision.date or scenario.state.get('requested_date'))
            await self._repository.checkpoint(replace(scenario, phase='failed', updated_at=self._now()), 'booking_flow_switched')
            return await self._start(
                connection,
                customer_id,
                update_id,
                decision=decision,
                origin_update_id=origin_update_id,
                requested_time_after=(
                    requested_time_after
                    or str(scenario.state.get("requested_time_after", ""))
                    or None
                ),
            )
        if decision.service and step != 'service':
            selected_service = str(scenario.state.get('service_name', '')).casefold().replace('ё', 'е')
            if decision.service.casefold().replace('ё', 'е').strip() != selected_service:
                return await self._refresh_current(connection, scenario)
            decision = replace(decision, service=None)
        if decision.service:
            query = decision.service.casefold().replace('ё', 'е').strip()
            choices = [item for item in scenario.state.get('choices', ()) if isinstance(item, Mapping)
                       and f' {query} ' in f" {str(item.get('label', '')).casefold().replace('ё', 'е')} "]
            if len(choices) != 1:
                return await self._refresh_current(connection, scenario)
            services = await self._catalog.list_services(connection, self._now())
            if not services:
                return BookingReply('Сейчас не могу подтвердить актуальные услуги. Попробуйте позже или напишите администратору.', main_menu_options())
            fresh_choices = self._service_choices(services)
            selected = choices[0]
            index = next((i for i, item in enumerate(fresh_choices)
                          if (item.get('service_id'), item.get('walk_in')) ==
                          (selected.get('service_id'), selected.get('walk_in'))), None)
            if index is None:
                return BookingReply('Этой услуги больше нет в актуальном каталоге. Откройте список кнопкой «📅 Записаться».', main_menu_options())
            scenario = self._remember_booking_step(scenario)
            state = self._state(scenario)
            state['choices'] = fresh_choices
            if decision.date:
                state['requested_date'] = decision.date
            scenario = replace(scenario, state=state)
            return await self._choose_service(scenario, scenario.state['choices'][index])
        if decision.date:
            if scenario.state.get("step") in {"service", "staff", "available_date", "slot"}:
                scenario = self._remember_booking_step(scenario)
            state = self._state(scenario)
            state["requested_date"] = decision.date
            if requested_time_after is not None:
                state["requested_time_after"] = requested_time_after
            scenario = await self._checkpoint(scenario, state, "booking_date_requested")
            if state.get("service_id"):
                return await self._choose_staff(scenario, {"staff_id": state.get("staff_id"), "label": state.get("staff_name", "Любой специалист")})
        if decision.choice is not None:
            return await self._semantic_choice(connection, scenario, customer_id, user_id, update_id, decision.choice)
        step = scenario.state.get("step")
        if step == "contact":
            return await self._collect_contact(
                connection, scenario, user_id, text, kind, data
            )
        if step == "name" and kind == "text" and decision.action == "provide_name":
            name = text.strip()
            if not name:
                return BookingReply("Как вас зовут?", {})
            state = self._state(scenario)
            state["customer_name"] = name
            return await self._show_confirmation(scenario, state)
        return await self._refresh_current(connection, scenario)

    async def _semantic_choice(self, connection, scenario, customer_id, user_id, update_id, index):
        step = str(scenario.state.get("step"))
        if step not in {"service", "staff", "available_date", "slot", "booking_management", "booking_action"}:
            return await self._refresh_current(connection, scenario)
        return await self._handle_callback(connection, customer_id, user_id, update_id, self._callback(scenario, step, index))

    async def routing_context(self, customer_id: str) -> str:
        scenario = await self._repository.get_active_for_customer(customer_id)
        state = {"today": self._now().astimezone(MOSCOW).date().isoformat(), "active": False, 'mode': 'idle'}
        if scenario is not None:
            step = str(scenario.state.get('step', ''))
            browsing = step.startswith('catalog_')
            management = scenario.kind in {'cancel', 'reschedule'} or step in {'booking_management', 'booking_action'}
            choices = scenario.state.get('choices', ())
            page = max(0, min(int(scenario.state.get('page', 0)), max(0, (len(choices) - 1) // 8)))
            state.update({'mode': 'catalog_browse' if browsing else 'booking_management' if management else 'booking',
                          'active': not browsing, 'step': step, 'page': page,
                          "service": scenario.state.get("service_name"),
                          "date": scenario.state.get("requested_date") or scenario.state.get("selected_date"),
                          'requested_date': scenario.state.get('requested_date'),
                          'selected_date': scenario.state.get('selected_date'),
                          "choices": [{'index': index, 'label': str(item.get('label', ''))[:128]}
                                      for index, item in enumerate(choices[page * 8:(page + 1) * 8], start=page * 8)
                                      if isinstance(item, Mapping)]})
            if not browsing:
                state['kind'] = scenario.kind
        return bound_routing_state(json.dumps(state, ensure_ascii=False)) or '{}'

    async def _start_management(
        self, customer_id: str, update_id: str, *, operation: str = "view",
        origin_update_id: str | None = None,
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
                    "label": f"{format_booking_time(booking.starts_at)} — {service_name}",
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
                "management_operation": operation,
                "origin_update_id": origin_update_id or update_id,
                "source": "telegram",
                "choices": choices,
            },
            error_code=None,
            created_at=self._now(),
            updated_at=self._now(),
        )
        scenario_id = await self._repository.create_scenario(scenario)
        scenario = await self._repository.get_scenario(scenario_id)
        if scenario.phase != "collecting":
            active = await self._repository.get_active_for_customer(customer_id)
            return self._render_current(active) if active is not None else BookingReply(STALE_REPLY, main_menu_options())
        if len(choices) == 1:
            return await self._choose_owned_booking(scenario, choices[0])
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
        *, decision: RouteDecision | None = None,
        origin_update_id: str | None = None,
        requested_time_after: str | None = None,
    ) -> BookingReply:
        services = await self._catalog.list_services(connection, self._now())
        if not services:
            return BookingReply(
                "Сейчас не могу загрузить услуги. Напишите администратору.", {}
            )
        choices = self._service_choices(services)
        if decision is not None and decision.service:
            query = decision.service.casefold().replace("ё", "е").strip()
            choices = [item for item in choices if f" {query} " in f" {str(item['label']).casefold().replace('ё', 'е')} "]
            if not choices:
                return BookingReply("Не нашёл такую услугу в каталоге. Уточните её название или откройте список кнопкой «📅 Записаться».", main_menu_options())
        scenario = BookingScenario(
            id=uuid4(),
            kind="create",
            phase="collecting",
            idempotency_key=f"telegram:create:{update_id}",
            customer_id=customer_id,
            state={
                "step": "service",
                "origin_update_id": origin_update_id or update_id,
                "source": "telegram",
                "choices": choices,
                "requested_date": decision.date if decision is not None else None,
                "requested_time_after": requested_time_after,
            },
            error_code=None,
            created_at=self._now(),
            updated_at=self._now(),
        )
        try:
            scenario_id = await self._repository.create_scenario(scenario)
            scenario = await self._repository.get_scenario(scenario_id)
        except asyncpg.UniqueViolationError:
            active = await self._repository.get_active_for_customer(customer_id)
            if active is None:
                raise
            return self._render_current(active)
        if scenario.phase != "collecting":
            return await self._recover_callback(connection, customer_id, update_id)
        if decision is not None and decision.service and len(choices) == 1:
            return await self._choose_service(scenario, scenario.state["choices"][0])
        return self._render_current(scenario)

    async def _start_catalog(self, connection, customer_id, update_id, *, origin_update_id=None):
        services = await self._catalog.list_services(connection, self._now())
        if not services:
            return BookingReply("Сейчас не могу подтвердить актуальные цены. Попробуйте позже или напишите администратору.", main_menu_options())
        categories = sorted({service.category_name or "Другие услуги" for service in services})
        scenario = BookingScenario(
            uuid4(), "create", "collecting", f"telegram:catalog:{update_id}", customer_id,
            {"source": "telegram", "origin_update_id": origin_update_id or update_id, "step": "catalog_category", "choices": [
                {"label": service_display_name(category), "category": category} for category in categories]},
            None, self._now(), self._now(),
        )
        scenario_id = await self._repository.create_scenario(scenario)
        current = await self._repository.get_scenario(scenario_id)
        if current.phase != "collecting":
            return await self._recover_callback(connection, customer_id, update_id)
        return await self._refresh_current(connection, current)

    async def _recover_catalog_root(self, scenario, services):
        current = await self._catalog_root(scenario, services)
        return BookingReply(
            f"{OUTDATED_BUTTON_REPLY}\n\n{current.text}",
            current.delivery_options,
        )

    async def _catalog_root(self, scenario, services):
        state = self._state(scenario)
        for key in ("category", "catalog_family", "catalog_service_id", "detail", "service_name"):
            state.pop(key, None)
        categories = sorted({service.category_name or "Другие услуги" for service in services})
        state.update(
            step="catalog_category",
            choices=[{"label": service_display_name(category), "category": category} for category in categories],
            page=0,
        )
        updated = await self._checkpoint(scenario, state, "catalog_view_selected")
        return self._render_current(updated, catalog_fresh=True)

    async def _catalog_choice(self, connection, scenario, action, choice):
        services = await self._catalog.list_services(connection, self._now())
        if not services:
            return BookingReply("Сейчас не могу подтвердить актуальные цены. Откройте «✨ Услуги и цены» позже.", main_menu_options())
        state = self._state(scenario)
        if action == "catalog_category":
            selected = [s for s in services if (s.category_name or "Другие услуги") == choice["category"]]
            if not selected:
                return await self._recover_catalog_root(scenario, services)
            choices = self._catalog_category_choices(selected)
            state.pop("catalog_family", None)
            state.update(step="catalog_service", category=choice["category"], choices=choices)
            if len(selected) == 1 and walk_in_family(selected[0].service_name) is None:
                return await self._catalog_choice(connection, replace(scenario, state=state), "catalog_service", {"service_id": selected[0].service_id})
            if len(choices) == 1 and choices[0].get("family"):
                family = str(choices[0]["family"])
                state.update(
                    catalog_family=family,
                    choices=self._catalog_family_choices(selected, family),
                )
            state["page"] = min(int(state.get("page", 0)), max(0, (len(state["choices"]) - 1) // 8))
        else:
            family = choice.get("family")
            if isinstance(family, str):
                previous_family = state.get("catalog_family")
                selected = [
                    service for service in services
                    if (service.category_name or "Другие услуги") == state.get("category")
                ]
                choices = self._catalog_family_choices(selected, family)
                if not choices:
                    return await self._recover_catalog_root(scenario, services)
                state.update(step="catalog_service", catalog_family=family, choices=choices)
                state["page"] = (
                    min(int(state.get("page", 0)), max(0, (len(choices) - 1) // 8))
                    if previous_family == family else 0
                )
                updated = await self._checkpoint(scenario, state, "catalog_view_selected")
                return self._render_current(updated, catalog_fresh=True)
            if action == "catalog_book" and choice.get("address") is True:
                return BookingReply(
                    "Мы находимся: Тульская область, Новомосковск, "
                    "ул. Трудовые резервы, 33Б, ТРЦ «Первый», цокольный этаж. "
                    "Работаем ежедневно с 10:00 до 21:00.",
                    main_menu_options(),
                )
            service = next((s for s in services if s.service_id == choice["service_id"]), None)
            if service is None:
                return BookingReply("Этой услуги больше нет в актуальном каталоге. Откройте «✨ Услуги и цены».", main_menu_options())
            family = walk_in_family(service.service_name)
            if action == "catalog_book" and choice.get("details") is True:
                state.update(detail=procedure_details(service.service_name), catalog_details=True)
                updated = await self._checkpoint(scenario, state, "catalog_details_selected")
                return self._render_current(updated, catalog_fresh=True)
            if action == "catalog_book":
                if family:
                    return BookingReply("Предварительная запись на эту услугу не нужна. Можно прийти ежедневно с 10:00 до 21:00.", main_menu_options())
                state.update(step="service", choices=[self._service_choice(service)])
                updated = await self._checkpoint(scenario, state, "catalog_booking_started")
                return await self._choose_service(updated, updated.state["choices"][0])
            detail = CatalogGrounding("fresh", (service,), "price", False).direct_reply
            detail = service_display_name(detail) + "\n\n" + procedure_description(service.service_name)
            if family:
                detail += "\nПредварительная запись не нужна — приходите ежедневно с 10:00 до 21:00."
            state.update(
                step="catalog_book",
                detail=detail,
                catalog_details=False,
                catalog_service_id=service.service_id,
                service_name=service.service_name,
                choices=(
                    [{"label": "Подробнее о процедуре", "service_id": service.service_id, "details": True},
                     {"label": "Как нас найти", "address": True}]
                    if family
                    else [
                        {"label": "Выбрать время", "service_id": service.service_id},
                        {"label": "Подробнее о процедуре", "service_id": service.service_id, "details": True},
                    ]
                ),
            )
        updated = await self._checkpoint(scenario, state, "catalog_view_selected")
        return self._render_current(updated, catalog_fresh=True)

    async def _refresh_current(self, connection, scenario):
        step = scenario.state.get("step")
        if step == "catalog_service":
            if scenario.state.get("catalog_family"):
                return await self._catalog_choice(
                    connection,
                    scenario,
                    "catalog_service",
                    {"family": scenario.state["catalog_family"]},
                )
            return await self._catalog_choice(connection, scenario, "catalog_category", {"category": scenario.state["category"]})
        if step == "catalog_book":
            return await self._catalog_choice(connection, scenario,
                "catalog_book" if scenario.state.get("catalog_details") else "catalog_service",
                {"service_id": scenario.state["catalog_service_id"], "details": bool(scenario.state.get("catalog_details"))})
        return self._render_current(scenario)

    @staticmethod
    def _price_summary(service):
        prices = [p for v in service.variants for p in (v.price_min, v.price_max)]
        price = _range_text(min(prices), max(prices), suffix="₽")
        if not service_duration_is_meaningful(service.service_name):
            return f"{service.service_name} — {price}"
        durations = [v.duration_minutes for v in service.variants]
        duration = _range_text(min(durations), max(durations), suffix="мин.")
        return f"{service.service_name} — {price} · {duration}"

    @classmethod
    def _catalog_category_choices(cls, services):
        families = {
            family for service in services
            if (family := walk_in_family(service.service_name)) is not None
        }
        return [
            {
                "label": label,
                "family": family,
                "summary": f"{label} — выберите длительность",
            }
            for family, label in _WALK_IN_LABELS.items()
            if family in families
        ] + [
            {
                "label": service_display_name(service.service_name),
                "button_label": (f"{service.variants[0].duration_minutes} минут" if re.fullmatch(r"водородотерапия\s+\d+\s+минут\w*", service.service_name, re.I) else service_display_name(cls._price_summary(service))),
                "service_id": service.service_id,
                "summary": cls._price_summary(service),
            }
            for service in services
            if walk_in_family(service.service_name) is None
        ]

    @classmethod
    def _catalog_family_choices(cls, services, family):
        return [
            {
                "label": service.service_name,
                "button_label": cls._catalog_family_button_label(service),
                "service_id": service.service_id,
                "summary": cls._price_summary(service),
            }
            for service in sorted(
                (
                    item for item in services
                    if walk_in_family(item.service_name) == family
                ),
                key=lambda item: (
                    min(variant.duration_minutes for variant in item.variants),
                    item.service_name.casefold(),
                    item.service_id,
                ),
            )
        ]

    @staticmethod
    def _catalog_family_button_label(service):
        minutes = min(variant.duration_minutes for variant in service.variants)
        prices = [
            price
            for variant in service.variants
            for price in (variant.price_min, variant.price_max)
        ]
        price = _range_text(min(prices), max(prices), suffix="₽")
        return f"{minutes} мин · {price}"

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
            return await self._recover_callback(
                connection, customer_id, update_id
            )
        scenario_id, action, index, revision = parsed
        scenario = await self._repository.get_scenario(scenario_id)
        if scenario is None or scenario.customer_id != customer_id:
            return await self._recover_callback(
                connection, customer_id, update_id
            )
        catalog_callback = action.startswith("catalog_") or (
            action == "page"
            and str(scenario.state.get("step", "")).startswith("catalog_")
        )
        if scenario.phase == "confirmed" and action in {"confirm", "confirm_change"} and scenario.state.get("confirmation_update_id") != update_id:
            return BookingReply("", {})
        if (catalog_callback and scenario.phase == "collecting"
                and scenario.state.get("last_catalog_callback") == raw_callback):
            if (scenario.state.get("last_catalog_update_id") != update_id
                    and self._now() - scenario.updated_at < timedelta(seconds=3)):
                return BookingReply("", {})
            return await self._refresh_current(connection, scenario)
        if revision != self._callback_revision(scenario):
            if catalog_callback and scenario.phase != "collecting":
                return BookingReply(
                    "Эта кнопка меню уже неактуальна. Откройте «✨ Услуги и цены».",
                    main_menu_options(),
                )
            current = await self._recover_callback(connection, customer_id, update_id)
            return BookingReply(
                f"{OUTDATED_BUTTON_REPLY}\n\n{current.text}",
                current.delivery_options,
            )
        if action == "cancel_draft" and scenario.phase == "collecting":
            return await self._cancel_draft(scenario)
        if action == "booking_back" and index == 0 and scenario.phase in {"collecting", "awaiting_confirmation"}:
            history = scenario.state.get("booking_history", ())
            if not history or str(scenario.state.get("step", "")).startswith("catalog_"):
                return await self._refresh_current(connection, scenario)
            state = self._state_item(history[-1])
            state["booking_history"] = list(history[:-1])
            if state.get("step") == "service":
                services = await self._catalog.list_services(connection, self._now())
                state["choices"] = self._service_choices(services)
            current = await self._checkpoint(replace(scenario, phase="collecting"), state, "booking_step_back")
            return await self._refresh_current(connection, current)
        if action == "clear_time_after" and scenario.phase == "collecting":
            state = self._state(scenario)
            state.pop("requested_time_after", None)
            current = await self._checkpoint(
                scenario, state, "booking_time_limit_cleared"
            )
            selected_date = current.state.get("selected_date")
            if selected_date:
                return await self._choose_date(current, {"date": selected_date})
            return await self._refresh_current(connection, current)
        if action == "choose_date" and scenario.phase == "collecting":
            state = self._state(scenario)
            state.update(step="available_date", choices=[])
            state.pop("selected_date", None)
            state.pop("requested_date", None)
            current = await self._checkpoint(
                scenario, state, "booking_date_reselection_requested"
            )
            return self._choice_reply(
                current,
                "Выберите другую дату: напишите, например, «завтра» или «10 сентября».",
                "available_date",
            )
        if action == "catalog_root" and scenario.phase == "collecting":
            services = await self._catalog.list_services(connection, self._now())
            if not services:
                return BookingReply(
                    "Сейчас не могу подтвердить актуальные цены. Попробуйте позже.",
                    main_menu_options(),
                )
            return await self._catalog_root(self._remember_catalog_callback(scenario, raw_callback, update_id), services)
        if action == "catalog_back" and scenario.phase == "collecting" and scenario.state.get("category"):
            return await self._catalog_choice(connection, self._remember_catalog_callback(scenario, raw_callback, update_id),
                "catalog_service" if scenario.state.get("catalog_family") else "catalog_category",
                {"family": scenario.state["catalog_family"]} if scenario.state.get("catalog_family") else {"category": scenario.state["category"]})
        if action == "page" and scenario.phase == "collecting":
            choices = scenario.state.get("choices", ())
            page_size = 10 if scenario.state.get("step") == "catalog_category" else 8
            if not 0 <= index <= (len(choices) - 1) // page_size:
                return await self._refresh_current(connection, scenario)
            if catalog_callback:
                scenario = self._remember_catalog_callback(scenario, raw_callback, update_id)
            state = self._state(scenario)
            state["page"] = index
            current = await self._checkpoint(scenario, state, "booking_page_selected")
            return await self._refresh_current(connection, current)
        if scenario.phase == "awaiting_confirmation" and action in {"confirm", "confirm_change"} and index == 0:
            state = self._state(scenario)
            state["confirmation_update_id"] = update_id
            scenario = await self._checkpoint(scenario, state, "booking_confirmation_received")
        if (
            action == "confirm"
            and index == 0
            and scenario.state.get("step") == "confirm"
            and scenario.phase in {"awaiting_confirmation", "confirmed"}
        ):
            result = await self._booking_service.handle(
                scenario.id,
                confirmed=True,
            )
            if result.next_action == "choose_slot":
                current = await self._repository.get_scenario(scenario.id)
                return await self._choose_staff(current, {"staff_id": current.state.get("staff_id"), "label": current.state.get("staff_name", "Любой специалист")})
            return BookingReply(result.message, main_menu_options())
        if (
            action == "confirm_change"
            and index == 0
            and scenario.state.get("step") == "confirm_change"
            and scenario.phase in {"awaiting_confirmation", "confirmed"}
        ):
            result = await self._booking_service.handle(
                scenario.id,
                confirmed=True,
                identity=BookingIdentity(customer_id, confirmed=True),
            )
            if result.next_action == "choose_slot":
                current = await self._repository.get_scenario(scenario.id)
                return await self._choose_staff(current, {"staff_id": current.state.get("staff_id"), "label": current.state.get("staff_name", "Любой специалист")})
            return BookingReply(result.message, main_menu_options())
        if scenario.phase != "collecting" or scenario.state.get("step") != action:
            if catalog_callback:
                return BookingReply(
                    "Эта кнопка меню уже неактуальна. Откройте «✨ Услуги и цены».",
                    main_menu_options(),
                )
            return await self._recover_callback(
                connection, customer_id, update_id
            )
        choices = scenario.state.get("choices")
        if not isinstance(choices, tuple) or not 0 <= index < len(choices):
            return await self._recover_callback(
                connection, customer_id, update_id
            )
        choice = choices[index]
        if not isinstance(choice, Mapping):
            return await self._recover_callback(
                connection, customer_id, update_id
            )
        if action in {"catalog_category", "catalog_service", "catalog_book"}:
            return await self._catalog_choice(connection, self._remember_catalog_callback(scenario, raw_callback, update_id), action, choice)
        if action in {"service", "staff", "available_date", "slot"}:
            scenario = self._remember_booking_step(scenario)
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
        return await self._recover_callback(connection, customer_id, update_id)

    def _remember_catalog_callback(self, scenario, raw_callback, update_id):
        # Persist together with the resulting view, so an inbox retry can recover it.
        state = self._state(scenario)
        state.update(last_catalog_callback=raw_callback, last_catalog_update_id=update_id)
        return replace(scenario, state=state)

    def _remember_booking_step(self, scenario):
        state = self._state(scenario)
        previous = {key: value for key, value in state.items() if key not in {"booking_history", "view_revision"}}
        state["booking_history"] = [*state.get("booking_history", ()), previous][-12:]
        return replace(scenario, state=state)

    async def _cancel_draft(self, scenario: BookingScenario | None) -> BookingReply:
        if scenario is None:
            return BookingReply("Сейчас нет незавершённого оформления.", main_menu_options())
        cancelled = replace(
            scenario,
            phase="failed",
            error_code="user_cancelled",
            updated_at=self._now(),
        )
        await self._repository.checkpoint(cancelled, "booking_flow_cancelled")
        return BookingReply(
            "Оформление остановлено. Новая запись не создана.",
            main_menu_options(),
        )

    @staticmethod
    def _requested_time_after(text: str) -> str | None:
        match = _TIME_AFTER.search(text)
        if match is None:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if hour > 23 or minute > 59:
            return None
        return f"{hour:02}:{minute:02}"

    async def _recover_callback(
        self,
        connection: asyncpg.Connection,
        customer_id: str,
        update_id: str,
    ) -> BookingReply:
        active = await self._repository.get_active_for_customer(customer_id)
        if active is not None:
            return await self._refresh_current(connection, active)
        return BookingReply(STALE_REPLY, main_menu_options())

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
            return await self._begin_change(updated, {"operation": operation})
        label = str(choice.get("label", "Запись"))
        return self._choice_reply(
            updated,
            f"Запись: {label}\nЧто сделать?",
            "booking_action",
        )

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
                f"Отменить запись на {format_booking_time(str(selected['starts_at']))}?",
                self._booking_options(scenario,
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
        walk_in = choice.get("walk_in")
        if isinstance(walk_in, str) and walk_in in _WALK_IN_LABELS:
            await self._repository.checkpoint(
                replace(scenario, updated_at=self._now()),
                "booking_walk_in_selected",
            )
            return BookingReply(
                f"{_WALK_IN_LABELS[walk_in]}: предварительная запись не нужна. "
                "Можно прийти ежедневно с 10:00 до 21:00. Выберите другую "
                "услугу в списке, если хотите продолжить.",
                self._choice_options(scenario, "service"),
            )
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
        if len(variants) == 1:
            only = variants[0]
            return await self._choose_staff(
                updated,
                {"staff_id": only["staff_id"], "label": only["label"]},
            )
        if state.get("requested_date"):
            return await self._choose_staff(updated, {"staff_id": None, "label": "Любой специалист"})
        return self._choice_reply(updated, "Выберите специалиста", "staff")

    async def _choose_staff(
        self, scenario: BookingScenario, choice: Mapping[str, object]
    ) -> BookingReply:
        # Changing date/time invalidates an earlier confirmation before querying slots.
        if scenario.phase != "collecting" or scenario.state.get("step") != "staff":
            state = self._state(scenario)
            state.update({"step": "staff", "choices": [
                {"staff_id": None, "label": "Любой специалист"},
                *[{"staff_id": key, "label": label} for key, label in state.get("staff_names", {}).items()],
            ]})
            scenario = await self._checkpoint(replace(scenario, phase="collecting"), state, "booking_time_reselection")
        now = self._now()
        requested = scenario.state.get("requested_date")
        start = datetime.fromisoformat(str(requested)).replace(tzinfo=MOSCOW) if requested else now
        end = start + timedelta(days=1) if requested else now + timedelta(days=14)
        if end <= now:
            return BookingReply("Эта дата уже прошла. Укажите будущую дату.", {})
        query = SlotQuery(
            (str(scenario.state["service_id"]),),
            max(now, start),
            end,
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
            value = slot.starts_at.astimezone(MOSCOW).date().isoformat()
            if value not in {item["date"] for item in dates}:
                dates.append({"date": value, "label": slot.starts_at.astimezone(MOSCOW).strftime("%d.%m")})
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
        if requested:
            return await self._choose_date(updated, {"date": requested})
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
        ]

        raw_time_after = scenario.state.get("requested_time_after")
        requested_time_after = (
            raw_time_after.strip() if isinstance(raw_time_after, str) else ""
        )
        if requested_time_after:
            slots = [
                item
                for item in slots
                if datetime.fromisoformat(str(item["starts_at"])).astimezone(MOSCOW).strftime("%H:%M")
                >= requested_time_after
            ]
        state = self._state(scenario)
        state.update(
            {"step": "slot", "selected_date": selected_date, "choices": slots}
        )
        updated = await self._checkpoint(scenario, state, "booking_date_selected")
        if requested_time_after and not slots:
            day = datetime.fromisoformat(selected_date).strftime("%d.%m.%Y")
            return BookingReply(
                f"После {requested_time_after} на {day} свободного времени нет. "
                "Можно посмотреть все часы или назвать другую дату.",
                self._booking_options(updated,
                    [
                        [("Показать всё время", self._callback(updated, "clear_time_after", 0))],
                        [("Выбрать другую дату", self._callback(updated, "choose_date", 0))],
                        [("Выйти из оформления", self._callback(updated, "cancel_draft", 0))],
                    ]
                ),
            )
        return self._choice_reply(updated, self._slot_header(updated), "slot")

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
                f"Перенести запись на {format_booking_time(str(choice['starts_at']))}?",
                self._booking_options(updated,
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
            f"{state['staff_name']}\n{format_booking_time(starts_at)}\n"
            f"{state['customer_name']}, {masked}"
        )
        return BookingReply(
            text,
            self._booking_options(updated,
                [[("Подтвердить", self._callback(updated, "confirm", 0))]]
            ),
        )

    async def _checkpoint(
        self, scenario: BookingScenario, state: Mapping[str, object], event: str
    ) -> BookingScenario:
        state = dict(state)
        if event != "booking_confirmation_received" and state.get("step") in {"service", "staff", "available_date", "slot", "contact", "name", "confirm", "confirm_change"}:
            state["view_revision"] = int(scenario.state.get("view_revision", 0)) + 1
        if state.get("step") != scenario.state.get("step"):
            state["page"] = 0
        updated = replace(scenario, state=state, updated_at=self._now())
        await self._repository.checkpoint(updated, event)
        return updated

    def _choice_reply(
        self, scenario: BookingScenario, text: str, action: str
    ) -> BookingReply:
        choices = scenario.state.get("choices")
        choices = choices if isinstance(choices, tuple) else ()
        page_size = 10 if action == "catalog_category" else 8
        page = min(int(scenario.state.get("page", 0)), max(0, (len(choices) - 1) // page_size))
        visible = [
            (str(choice.get("button_label", choice["label"])), self._callback(scenario, action, index))
            for index, choice in enumerate(choices)
            if page * page_size <= index < (page + 1) * page_size
            if isinstance(choice, Mapping)
        ]
        if action in {"catalog_service", "catalog_category"} and visible and all(
            len(label) <= 24 for label, _ in visible
        ):
            rows = [visible[index:index + 2] for index in range(0, len(visible), 2)]
        else:
            rows = [[item] for item in visible]
        navigation = []
        if page:
            navigation.append((
                "← Предыдущие" if action.startswith("catalog_") else "← Назад",
                self._callback(scenario, "page", page - 1),
            ))
        if (page + 1) * page_size < len(choices):
            navigation.append((
                "Ещё варианты →" if action.startswith("catalog_") else "Далее →",
                self._callback(scenario, "page", page + 1),
            ))
        if navigation:
            rows.append(navigation)
        if action in {"catalog_service", "catalog_book"}:
            if action == "catalog_book" and scenario.state.get("catalog_family"):
                rows.append([("← Длительности", self._callback(scenario, "catalog_back", 0))])
            else:
                rows.append([("← Категории", self._callback(scenario, "catalog_root", 0))])
        if action in {"service", "staff", "available_date", "slot"}:
            rows.append(
                [("Выйти из оформления", self._callback(scenario, "cancel_draft", 0))]
            )
        options = self._booking_options(scenario, rows) if action in {"service", "staff", "available_date", "slot"} else self._inline_options(rows)
        return BookingReply(text, options)

    def _booking_options(self, scenario, rows):
        rows = list(rows)
        if scenario.state.get("booking_history"):
            rows.append([("Назад", self._callback(scenario, "booking_back", 0))])
        return {**self._inline_options(rows), "booking_card": str(scenario.id)}

    def _choice_options(self, scenario: BookingScenario, action: str):
        return self._choice_reply(scenario, "", action).delivery_options

    def _render_current(self, scenario: BookingScenario, *, catalog_fresh=False) -> BookingReply:
        labels = {
            "service": "Выберите услугу",
            "staff": "Выберите специалиста",
            "available_date": "Выберите дату",
            "slot": "Выберите время",
            "contact": "Отправьте номер телефона.",
            "name": "Как вас зовут?",
            "confirm": "Подтвердите запись кнопкой ниже.",
            "booking_management": "Выберите запись",
            "booking_action": "Выберите действие",
            "catalog_category": "Услуги и цены\nВыберите категорию:",
        }
        step = str(scenario.state.get("step", ""))
        if step in {"catalog_service", "catalog_book"} and not catalog_fresh:
            return BookingReply("Откройте «✨ Услуги и цены», чтобы получить актуальные цены.", main_menu_options())
        text = labels.get(step, STALE_REPLY)
        if step == "catalog_service":
            page = int(scenario.state.get("page", 0))
            choices = scenario.state.get("choices", ())[page * 8:(page + 1) * 8]
            pages = max(1, (len(scenario.state.get("choices", ())) + 7) // 8)
            category = service_display_name(str(scenario.state.get("category", "Услуги")))
            text = category + "\nВыберите вариант:"
            if pages > 1:
                text += f"\nСтраница {page + 1} из {pages}"
        elif step == "catalog_book":
            text = str(scenario.state.get("detail", ""))
        elif step == "slot":
            text = self._slot_header(scenario)
        elif step == "booking_action":
            selected = scenario.state.get("selected_booking")
            if isinstance(selected, Mapping):
                text = f"Запись: {selected.get('label', 'Запись')}\nЧто сделать?"
        elif step == "service" and scenario.state.get("requested_date"):
            day = datetime.fromisoformat(str(scenario.state["requested_date"])).strftime("%d.%m.%Y")
            text = f"Покажу свободное время на {day}. Сначала уточните услугу: доступное время зависит от её вида, длительности и специалиста."
        if step == "confirm":
            return BookingReply(
                text,
                self._booking_options(scenario,
                    [[("Подтвердить", self._callback(scenario, "confirm", 0))]]
                ),
            )
        if step == "confirm_change":
            if scenario.kind == "cancel":
                starts_at = datetime.fromisoformat(str(scenario.state["starts_at"]))
                text = f"Отменить запись на {format_booking_time(starts_at)}?"
                label = "Да, отменить"
            elif scenario.kind == "reschedule":
                starts_at = datetime.fromisoformat(
                    str(scenario.state["new_starts_at"])
                )
                text = f"Перенести запись на {format_booking_time(starts_at)}?"
                label = "Да, перенести"
            else:
                return BookingReply(STALE_REPLY, {})
            return BookingReply(
                text,
                self._booking_options(scenario,
                    [[(label, self._callback(scenario, "confirm_change", 0))]]
                ),
            )
        if step in {
            "catalog_category", "catalog_service", "catalog_book",
            "service",
            "staff",
            "available_date",
            "slot",
            "booking_management",
            "booking_action",
        }:
            return self._choice_reply(scenario, text, step)
        return BookingReply(text, {})

    @staticmethod
    def _slot_header(scenario: BookingScenario) -> str:
        service = str(scenario.state.get("service_name", "")).strip()
        selected_date = str(scenario.state.get("selected_date", "")).strip()
        if not service or not selected_date:
            return "Выберите время"
        day = datetime.fromisoformat(selected_date).strftime("%d.%m.%Y")
        raw_time_after = scenario.state.get("requested_time_after")
        requested_time_after = (
            raw_time_after.strip() if isinstance(raw_time_after, str) else ""
        )
        suffix = f" · после {requested_time_after}" if requested_time_after else ""
        return f"Выберите время\n{service}\n{day} · московское время{suffix}"

    @staticmethod
    def _service_choice(service: CatalogService) -> dict[str, object]:
        return {
            "service_id": service.service_id,
            "label": service_display_name(service.service_name),
            "variants": [
                {"staff_id": variant.staff_id, "label": variant.staff_name}
                for variant in service.variants
            ],
        }

    @classmethod
    def _service_choices(
        cls, services: tuple[CatalogService, ...]
    ) -> list[dict[str, object]]:
        return [
            cls._service_choice(service)
            for service in services
            if walk_in_family(service.service_name) is None
        ]

    @staticmethod
    def _slot_choice(slot: Slot) -> dict[str, object]:
        return {
            "slot_id": slot.id,
            "starts_at": slot.starts_at.astimezone(MOSCOW).isoformat(),
            "staff_id": slot.staff_id,
            "label": slot.starts_at.astimezone(MOSCOW).strftime("%H:%M"),
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

    @classmethod
    def _callback_revision(cls, scenario: BookingScenario) -> str:
        state = cls._state(scenario)
        view = {key: state.get(key) for key in ("step", "choices", "selected_slot_id", "new_starts_at", "selected_booking", "requested_date", "selected_date", "requested_time_after")}
        if "view_revision" in state:
            view["view_revision"] = state["view_revision"]
        return hashlib.sha256(json.dumps(view, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]

    @classmethod
    def _callback(cls, scenario: BookingScenario, action: str, index: int) -> str:
        code = _CALLBACK_ACTIONS.index(action)
        return f"booking:v1:{scenario.id.hex}:{code}:{index}.{cls._callback_revision(scenario)}"

    @staticmethod
    def _parse_callback(raw: object) -> tuple[UUID, str, int, str | None] | None:
        if not isinstance(raw, str):
            return None
        parts = raw.split(":")
        if len(parts) != 5 or parts[:2] != ["booking", "v1"]:
            return None
        try:
            if "." not in parts[4]:
                return UUID(hex=parts[2]), parts[3], int(parts[4]), None
            index, revision = parts[4].split(".", 1)
            code = int(parts[3])
            if not 0 <= code < len(_CALLBACK_ACTIONS):
                return None
            return UUID(hex=parts[2]), _CALLBACK_ACTIONS[code], int(index), revision
        except (ValueError, TypeError, IndexError):
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
