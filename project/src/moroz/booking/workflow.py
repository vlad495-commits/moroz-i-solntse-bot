from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from moroz.booking.catalog import BookingCatalogPort
from moroz.booking.interaction import (
    BookingOwner,
    Interaction,
    WorkflowReply,
)
from moroz.booking.models import (
    BookingNotFound,
    BookingOutcomeUnknown,
    BookingTemporaryError,
    BookingIdentity,
    ExternalBooking,
    GetBooking,
    Slot,
    SlotQuery,
)
from moroz.booking.ports import BookingPort
from moroz.booking.presenter import BookingPresenter, PresentedAction
from moroz.booking.service import BookingService, booking_snapshots_match
from moroz.booking.workflow_repository import (
    BookingAction,
    BookingWorkflowRepository,
    WorkflowSession,
    WorkflowRevisionConflict,
)
from moroz.messaging.models import ScenarioResult


_ACTIVE_PHASES = {"collecting", "awaiting_confirmation", "executing"}
_COLLECTING_CREATE_PHASE_ERROR = "unsupported create phase: collecting"
_NAME_LIMIT = 100
_CALLBACK_PREFIX = "booking:"
_WORD_PATTERN = re.compile(r"[а-яё]+", re.IGNORECASE)
_PARTIAL_SERVICE_ACTION_STEMS = (
    "добав",
    "измен",
    "помен",
    "смен",
    "замен",
    "убра",
    "удал",
)
_OTHER_CHANGE_TARGET_STEMS = ("дат", "врем", "мастер")
_PROTECTED_UNAVAILABLE = (
    "Не удалось безопасно проверить ваши записи. Попробуйте позже."
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _is_partial_service_change(
    text: str,
    state: Mapping[str, object],
) -> bool:
    words = tuple(word.casefold() for word in _WORD_PATTERN.findall(text))
    action_indexes = tuple(
        index
        for index, word in enumerate(words)
        if word.startswith(_PARTIAL_SERVICE_ACTION_STEMS)
    )
    if not action_indexes:
        return False
    service_words = {
        word.casefold()
        for item in state.get("services", ())
        if isinstance(item, Mapping)
        for word in _WORD_PATTERN.findall(str(item.get("title", "")))
    }
    target_indexes = {
        index: "service"
        for index, word in enumerate(words)
        if word.startswith(("услуг", "процедур")) or word in service_words
    }
    target_indexes.update(
        {
            index: "other"
            for index, word in enumerate(words)
            if word.startswith(_OTHER_CHANGE_TARGET_STEMS)
        }
    )
    for action in action_indexes:
        following = (target for target in target_indexes if target > action)
        closest = min(following, default=None)
        if closest is None:
            preceding = tuple(
                target for target in target_indexes if target < action
            )
            preceding_other = (
                target
                for target in preceding
                if target_indexes[target] == "other"
            )
            closest = max(preceding_other, default=None)
            if closest is None:
                closest = max(preceding, default=None)
        if closest is not None and target_indexes[closest] == "service":
            return True
    return False


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("workflow payload text is invalid")
    return value


class BookingWorkflow:
    def __init__(
        self,
        catalog: BookingCatalogPort,
        booking_port: BookingPort,
        repository: BookingWorkflowRepository,
        booking_service: BookingService,
        *,
        now: Callable[[], datetime] = _utc_now,
        timezone: ZoneInfo | None = None,
        horizon_days: int = 14,
        confirmation_ttl_seconds: int = 1800,
        page_size: int = 6,
        presenter: BookingPresenter | None = None,
    ) -> None:
        if horizon_days != 14:
            raise ValueError("booking horizon must be exactly 14 days")
        if confirmation_ttl_seconds != 1800:
            raise ValueError("confirmation TTL must be exactly 30 minutes")
        if page_size < 1:
            raise ValueError("page size must be positive")
        self._catalog = catalog
        self._booking_port = booking_port
        self._repository = repository
        self._booking_service = booking_service
        self._now = now
        self._timezone = timezone or ZoneInfo("Europe/Moscow")
        self._horizon = timedelta(days=horizon_days)
        self._confirmation_ttl = timedelta(seconds=confirmation_ttl_seconds)
        self._page_size = page_size
        self._presenter = presenter or BookingPresenter()

    async def start_create(
        self,
        owner: BookingOwner,
        idempotency_key: str,
    ) -> WorkflowReply:
        session = await self._repository.start(
            "create",
            owner.channel,
            owner.chat_id,
            owner.customer_id,
            idempotency_key,
        )
        if (
            session.phase == "awaiting_confirmation"
            and session.expires_at is not None
            and session.expires_at <= self._aware_now()
        ):
            recovery = await self._slot_recovery(session)
            session = await self._repository.checkpoint(
                recovery,
                "booking_confirmation_expired",
                {},
            )
        if session.state.get("step") is not None:
            return await self._render_current(session)
        try:
            services = await self._catalog.list_services()
        except BookingTemporaryError:
            return self._unavailable()
        if not services:
            return self._unavailable()
        state = {
            "step": "services",
            "services": [
                {
                    "id": service.id,
                    "title": service.title,
                    "duration_minutes": service.duration_minutes,
                }
                for service in services
            ],
            "selected_service_ids": [],
            "service_page": 0,
        }
        session = await self._repository.checkpoint(
            replace(session, state=state),
            "booking_services_presented",
            {"service_count": len(services)},
        )
        return await self._render_services(session)

    async def list_bookings(self, owner: BookingOwner) -> WorkflowReply:
        bookings = await self._load_protected_bookings(owner.customer_id)
        if bookings is None:
            return self._presenter.plain(_PROTECTED_UNAVAILABLE)
        if not bookings:
            return self._presenter.plain("У вас нет предстоящих записей через бота.")
        summaries = [
            self._booking_summary(item, index + 1)
            for index, item in enumerate(bookings)
        ]
        return self._presenter.plain("Ваши записи:\n\n" + "\n\n".join(summaries))

    async def start_reschedule(
        self,
        owner: BookingOwner,
        idempotency_key: str,
    ) -> WorkflowReply:
        return await self._start_change("reschedule", owner, idempotency_key)

    async def start_cancel(
        self,
        owner: BookingOwner,
        idempotency_key: str,
    ) -> WorkflowReply:
        return await self._start_change("cancel", owner, idempotency_key)

    async def _start_change(
        self,
        kind: str,
        owner: BookingOwner,
        idempotency_key: str,
    ) -> WorkflowReply:
        session = await self._repository.start(
            kind,
            owner.channel,
            owner.chat_id,
            owner.customer_id,
            idempotency_key,
        )
        if session.state.get("step") is not None:
            return await self._render_current(session)
        bookings = await self._load_protected_bookings(owner.customer_id)
        if bookings is None:
            return self._presenter.plain(_PROTECTED_UNAVAILABLE)
        if not bookings:
            return self._presenter.plain("У вас нет предстоящих записей через бота.")
        state = {
            "step": "booking",
            "owned_bookings": bookings,
        }
        session = await self._repository.checkpoint(
            replace(session, state=state),
            "owned_bookings_presented",
            {"booking_count": len(bookings)},
        )
        return await self._render_booking_choices(session)

    async def handle(self, interaction: Interaction) -> WorkflowReply:
        if interaction.kind == "callback":
            return await self._handle_callback(interaction)
        owner = interaction.owner
        session = await self._repository.get_active(
            owner.channel,
            owner.chat_id,
            owner.customer_id,
        )
        if session is None:
            return self._refresh()
        if session.phase == "executing":
            return self._presenter.plain(
                "Запись уже завершается. Назад вернуться нельзя; дождитесь результата."
            )
        if interaction.kind == "text":
            return await self._handle_text(session, interaction)
        if interaction.kind == "contact":
            return await self._handle_contact(session, interaction)
        return self._refresh()

    async def _handle_callback(self, interaction: Interaction) -> WorkflowReply:
        callback_data = interaction.callback_data or ""
        if not callback_data.startswith(_CALLBACK_PREFIX):
            return self._refresh()
        action_id = callback_data.removeprefix(_CALLBACK_PREFIX)
        if not action_id or _CALLBACK_PREFIX in action_id:
            return self._refresh()
        owner = interaction.owner
        action = await self._repository.consume_action(
            action_id,
            owner.channel,
            owner.chat_id,
            owner.customer_id,
        )
        if action is None:
            return self._refresh()
        if action.result is not None:
            return WorkflowReply.from_result(action.result)
        if action.action_kind == "confirm":
            return await self._confirm(owner, action)
        session = await self._repository.get_active(
            owner.channel,
            owner.chat_id,
            owner.customer_id,
        )
        if (
            session is None
            or session.id != action.scenario_id
            or session.revision != action.revision
        ):
            return self._refresh()
        if session.phase == "executing":
            return self._presenter.plain(
                "Запись уже завершается. Назад вернуться нельзя; дождитесь результата."
            )
        handlers = {
            "select_service": self._select_service,
            "service_page": self._service_page,
            "services_done": self._services_done,
            "select_master": self._select_master,
            "date_page": self._date_page,
            "select_date": self._select_date,
            "slot_page": self._slot_page,
            "select_slot": self._select_slot,
            "select_booking": self._select_booking,
            "back": self._back,
            "change": self._change,
            "cancel": self._cancel,
        }
        handler = handlers.get(action.action_kind)
        if handler is None:
            return self._refresh()
        try:
            return await handler(session, action)
        except WorkflowRevisionConflict:
            return self._refresh()

    async def _handle_text(
        self,
        session: WorkflowSession,
        interaction: Interaction,
    ) -> WorkflowReply:
        text = (interaction.text_value or "").strip()
        if (
            session.kind in {"reschedule", "cancel"}
            and _is_partial_service_change(text, session.state)
        ):
            result = await self._booking_service.escalate(
                session.id,
                identity=BookingIdentity(session.customer_id, True),
                error_code="partial_service_change_unsupported",
            )
            return self._presenter.scenario_result(result, session.kind)
        if text.casefold() == "назад":
            return await self._back(session, None)
        if text.casefold() == "отмена":
            return await self._cancel(session, None)
        if session.state.get("step") != "customer_name":
            return self._refresh()
        if not text or len(text) > _NAME_LIMIT:
            return await self._render_name(
                session,
                "Укажите непустое имя длиной до 100 символов.",
            )
        state = self._state(session)
        state["customer_name"] = text
        state["step"] = "customer_contact"
        advanced = await self._repository.checkpoint(
            replace(session, state=state),
            "booking_customer_name_collected",
            {"name_length": len(text)},
        )
        return self._presenter.request_contact(
            "Отправьте свой контакт кнопкой ниже. Чужой контакт не принимается."
        )

    async def _handle_contact(
        self,
        session: WorkflowSession,
        interaction: Interaction,
    ) -> WorkflowReply:
        if session.state.get("step") != "customer_contact":
            return self._refresh()
        if interaction.contact_user_id != session.customer_id:
            return self._presenter.request_contact(
                "Можно отправить только свой контакт из этого Telegram-аккаунта."
            )
        if interaction.personal_data_processing_allowed is not True:
            return self._presenter.plain(
                "Для записи нужно согласие на обработку персональных данных."
            )
        phone = self._normalize_phone(interaction.phone_number or "")
        if phone is None:
            return self._presenter.request_contact(
                "Не удалось проверить номер. Отправьте свой контакт кнопкой ниже."
            )
        state = self._state(session)
        state["customer_phone"] = phone
        state["personal_data_processing_allowed"] = True
        state["step"] = "awaiting_confirmation"
        expires_at = self._aware_now() + self._confirmation_ttl
        advanced = await self._repository.checkpoint(
            replace(
                session,
                phase="awaiting_confirmation",
                state=state,
                expires_at=expires_at,
            ),
            "booking_confirmation_ready",
            {"confirmation_ttl_seconds": 1800},
        )
        return await self._render_summary(advanced)

    async def _select_service(
        self,
        session: WorkflowSession,
        action: BookingAction,
    ) -> WorkflowReply:
        if session.state.get("step") != "services":
            return self._refresh()
        service_id = _required_text(action.payload.get("service_id"))
        state = self._state(session)
        allowed = [item["id"] for item in state["services"]]
        if service_id not in allowed:
            return self._refresh()
        selected = list(state.get("selected_service_ids", []))
        if service_id in selected:
            selected.remove(service_id)
        else:
            selected.append(service_id)
        state["selected_service_ids"] = [
            item for item in allowed if item in set(selected)
        ]
        advanced = await self._repository.checkpoint(
            replace(session, state=state),
            "booking_service_selection_changed",
            {"selected_count": len(state["selected_service_ids"])},
        )
        return await self._render_services(advanced)

    async def _service_page(
        self,
        session: WorkflowSession,
        action: BookingAction,
    ) -> WorkflowReply:
        if session.state.get("step") != "services":
            return self._refresh()
        page = action.payload.get("page")
        if not isinstance(page, int) or page < 0:
            return self._refresh()
        state = self._state(session)
        count = len(state["services"])
        if page * self._page_size >= count:
            return self._refresh()
        state["service_page"] = page
        advanced = await self._repository.checkpoint(
            replace(session, state=state),
            "booking_services_page_changed",
            {"page": page},
        )
        return await self._render_services(advanced)

    async def _services_done(
        self,
        session: WorkflowSession,
        _action: BookingAction,
    ) -> WorkflowReply:
        if session.state.get("step") != "services":
            return self._refresh()
        state = self._state(session)
        selected = tuple(state.get("selected_service_ids", []))
        if not selected:
            return self._presenter.plain("Выберите хотя бы одну услугу.")
        try:
            staff = await self._catalog.list_staff(selected)
        except BookingTemporaryError:
            return self._unavailable()
        if not staff:
            return self._presenter.plain(
                "Эта комбинация услуг сейчас недоступна для общей записи."
            )
        state["staff"] = [
            {
                "id": member.id,
                "name": member.name,
                "service_ids": list(member.service_ids),
            }
            for member in staff
        ]
        state["step"] = "master"
        advanced = await self._repository.checkpoint(
            replace(session, state=state),
            "booking_services_selected",
            {"service_count": len(selected), "staff_count": len(staff)},
        )
        return await self._render_master(advanced)

    async def _select_master(
        self,
        session: WorkflowSession,
        action: BookingAction,
    ) -> WorkflowReply:
        if session.state.get("step") != "master":
            return self._refresh()
        raw_staff_id = action.payload.get("staff_id")
        if raw_staff_id is not None and not isinstance(raw_staff_id, str):
            return self._refresh()
        state = self._state(session)
        staff_ids = {item["id"] for item in state.get("staff", [])}
        if raw_staff_id is not None and raw_staff_id not in staff_ids:
            return self._refresh()
        now = self._aware_now()
        query = SlotQuery(
            service_ids=tuple(state["selected_service_ids"]),
            starts_after=now,
            starts_before=now + self._horizon,
            staff_id=raw_staff_id,
        )
        try:
            slots = await self._booking_port.list_slots(query)
        except BookingTemporaryError:
            return self._unavailable()
        slots = self._allowed_slots(slots, query, staff_ids)
        if not slots:
            return self._presenter.plain(
                "Свободное время на ближайшие 14 дней сейчас недоступно."
            )
        state["staff_choice"] = raw_staff_id
        state["slot_query"] = {
            "service_ids": list(query.service_ids),
            "starts_after": query.starts_after.isoformat(),
            "starts_before": query.starts_before.isoformat(),
            "staff_id": query.staff_id,
        }
        state["slots"] = [self._slot_state(slot) for slot in slots]
        state["date_page"] = 0
        state["step"] = "date"
        advanced = await self._repository.checkpoint(
            replace(session, state=state),
            "booking_master_selected",
            {"any_master": raw_staff_id is None, "slot_count": len(slots)},
        )
        return await self._render_dates(advanced)

    async def _date_page(
        self,
        session: WorkflowSession,
        action: BookingAction,
    ) -> WorkflowReply:
        if session.state.get("step") != "date":
            return self._refresh()
        page = action.payload.get("page")
        dates = self._dates(session.state)
        if (
            not isinstance(page, int)
            or page < 0
            or page * self._page_size >= len(dates)
        ):
            return self._refresh()
        state = self._state(session)
        state["date_page"] = page
        advanced = await self._repository.checkpoint(
            replace(session, state=state),
            "booking_dates_page_changed",
            {"page": page},
        )
        return await self._render_dates(advanced)

    async def _select_date(
        self,
        session: WorkflowSession,
        action: BookingAction,
    ) -> WorkflowReply:
        if session.state.get("step") != "date":
            return self._refresh()
        date_key = _required_text(action.payload.get("date"))
        if date_key not in self._dates(session.state):
            return self._refresh()
        state = self._state(session)
        state["selected_date"] = date_key
        state["slot_page"] = 0
        state["step"] = "slot"
        advanced = await self._repository.checkpoint(
            replace(session, state=state),
            "booking_date_selected",
            {},
        )
        return await self._render_slots(advanced)

    async def _slot_page(
        self,
        session: WorkflowSession,
        action: BookingAction,
    ) -> WorkflowReply:
        if session.state.get("step") != "slot":
            return self._refresh()
        page = action.payload.get("page")
        slots = self._slots_for_date(session.state)
        if (
            not isinstance(page, int)
            or page < 0
            or page * self._page_size >= len(slots)
        ):
            return self._refresh()
        state = self._state(session)
        state["slot_page"] = page
        advanced = await self._repository.checkpoint(
            replace(session, state=state),
            "booking_slots_page_changed",
            {"page": page},
        )
        return await self._render_slots(advanced)

    async def _select_slot(
        self,
        session: WorkflowSession,
        action: BookingAction,
    ) -> WorkflowReply:
        if session.state.get("step") != "slot":
            return self._refresh()
        slot_id = _required_text(action.payload.get("slot_id"))
        slot = next(
            (
                item
                for item in self._slots_for_date(session.state)
                if item["id"] == slot_id
            ),
            None,
        )
        if slot is None:
            return self._refresh()
        staff = {
            item["id"]: item["name"] for item in session.state.get("staff", ())
        }
        staff_name = staff.get(slot["staff_id"])
        if not isinstance(staff_name, str):
            return self._refresh()
        state = self._state(session)
        state["selected_slot_id"] = slot_id
        state["actual_staff_id"] = slot["staff_id"]
        state["actual_staff_name"] = staff_name
        state["duration_minutes"] = slot["duration_minutes"]
        if session.kind == "reschedule":
            state["selected_new_starts_at"] = slot["starts_at"]
            state["step"] = "awaiting_confirmation"
            expires_at = self._aware_now() + self._confirmation_ttl
            advanced = await self._repository.checkpoint(
                replace(
                    session,
                    phase="awaiting_confirmation",
                    state=state,
                    expires_at=expires_at,
                ),
                "booking_confirmation_ready",
                {"confirmation_ttl_seconds": 1800},
            )
            return await self._render_summary(advanced)
        state["starts_at"] = slot["starts_at"]
        state["step"] = "customer_name"
        advanced = await self._repository.checkpoint(
            replace(session, state=state),
            "booking_slot_selected",
            {"duration_minutes": slot["duration_minutes"]},
        )
        return await self._render_name(advanced, "Как вас зовут?")

    async def _select_booking(
        self,
        session: WorkflowSession,
        action: BookingAction,
    ) -> WorkflowReply:
        if session.state.get("step") != "booking":
            return self._refresh()
        index = action.payload.get("booking_index")
        bookings = session.state.get("owned_bookings", ())
        if not isinstance(index, int) or index < 0 or index >= len(bookings):
            return self._refresh()
        selected = bookings[index]
        if not isinstance(selected, Mapping):
            return self._refresh()
        state = self._state(session)
        state.update(
            {
                "external_id": _required_text(selected.get("external_id")),
                "booking_key": _required_text(selected.get("booking_key")),
                "original_slot_id": _required_text(selected.get("slot_id")),
                "starts_at": _required_text(selected.get("starts_at")),
                "old_starts_at": _required_text(selected.get("starts_at")),
                "old_scheduled_end_at": selected.get("scheduled_end_at"),
                "selected_service_ids": list(selected.get("service_ids", ())),
                "services": list(selected.get("services", ())),
                "old_staff_id": _required_text(selected.get("staff_id")),
                "old_staff_name": _required_text(selected.get("staff_name")),
            }
        )
        if session.kind == "cancel":
            state["step"] = "awaiting_confirmation"
            expires_at = self._aware_now() + self._confirmation_ttl
            advanced = await self._repository.checkpoint(
                replace(
                    session,
                    phase="awaiting_confirmation",
                    state=state,
                    expires_at=expires_at,
                ),
                "booking_confirmation_ready",
                {"confirmation_ttl_seconds": 1800},
            )
            return await self._render_summary(advanced)
        try:
            staff = await self._catalog.list_staff(
                tuple(state["selected_service_ids"])
            )
        except BookingTemporaryError:
            return self._unavailable()
        if not staff:
            return self._unavailable()
        state["staff"] = [
            {
                "id": member.id,
                "name": member.name,
                "service_ids": list(member.service_ids),
            }
            for member in staff
        ]
        state["step"] = "master"
        advanced = await self._repository.checkpoint(
            replace(session, state=state),
            "booking_selected_for_reschedule",
            {"service_count": len(state["selected_service_ids"])},
        )
        return await self._render_master(advanced)

    async def _back(
        self,
        session: WorkflowSession,
        _action: BookingAction | None,
    ) -> WorkflowReply:
        if session.phase == "awaiting_confirmation" and session.kind != "create":
            state = self._state(session)
            state["step"] = "slot" if session.kind == "reschedule" else "booking"
            advanced = await self._repository.checkpoint(
                replace(
                    session,
                    phase="collecting",
                    state=state,
                    expires_at=None,
                ),
                "booking_workflow_back",
                {"step": state["step"]},
            )
            return await self._render_current(advanced)
        if session.phase != "collecting":
            return self._presenter.plain(
                "После начала подтверждения назад вернуться нельзя."
            )
        step = session.state.get("step")
        previous = {
            "master": "booking" if session.kind == "reschedule" else "services",
            "date": "master",
            "slot": "date",
            "customer_name": "slot",
            "customer_contact": "customer_name",
        }.get(step)
        if previous is None:
            return self._refresh()
        state = self._state(session)
        state["step"] = previous
        advanced = await self._repository.checkpoint(
            replace(session, state=state),
            "booking_workflow_back",
            {"step": previous},
        )
        return await self._render_current(advanced)

    async def _change(
        self,
        session: WorkflowSession,
        _action: BookingAction,
    ) -> WorkflowReply:
        if session.phase != "awaiting_confirmation":
            return self._refresh()
        state = self._state(session)
        for key in tuple(state):
            if key not in {"services"}:
                state.pop(key)
        state.update(
            {
                "step": "services",
                "selected_service_ids": [],
                "service_page": 0,
            }
        )
        advanced = await self._repository.checkpoint(
            replace(
                session,
                phase="collecting",
                state=state,
                expires_at=None,
            ),
            "booking_confirmation_changed",
            {},
        )
        return await self._render_services(advanced)

    async def _cancel(
        self,
        session: WorkflowSession,
        _action: BookingAction | None,
    ) -> WorkflowReply:
        if session.phase not in {"collecting", "awaiting_confirmation"}:
            return self._refresh()
        await self._repository.checkpoint(
            replace(session, phase="cancelled", expires_at=None),
            "booking_workflow_cancelled",
            {},
        )
        return self._presenter.plain(
            "Действие отменено. Изменения не отправлялись."
        )

    async def _confirm(
        self,
        owner: BookingOwner,
        action: BookingAction,
    ) -> WorkflowReply:
        session = await self._repository.get(action.scenario_id)
        if session is None:
            return self._refresh()
        if session.phase == "collecting":
            if not self._is_confirm_slot_recovery(session, action):
                return self._refresh()
            result = ScenarioResult(
                "needs_input",
                "",
                "choose_slot",
                (),
            )
        else:
            try:
                if session.kind == "create":
                    result = await self._booking_service.handle(
                        action.scenario_id,
                        confirmed=True,
                    )
                else:
                    result = await self._booking_service.handle(
                        action.scenario_id,
                        confirmed=True,
                        identity=BookingIdentity(owner.customer_id, True),
                    )
            except ValueError as error:
                if error.args != (_COLLECTING_CREATE_PHASE_ERROR,):
                    raise
                observed = await self._repository.consume_action(
                    action.id,
                    owner.channel,
                    owner.chat_id,
                    owner.customer_id,
                )
                if observed is not None and observed.result is not None:
                    return WorkflowReply.from_result(observed.result)
                latest = await self._repository.get(action.scenario_id)
                if not self._is_confirm_slot_recovery(latest, action):
                    raise
                result = ScenarioResult(
                    "needs_input",
                    "",
                    "choose_slot",
                    (),
                )
        reply = self._presenter.scenario_result(result, session.kind)
        recovery_session = None
        if result.status == "needs_input" and result.next_action == "choose_slot":
            latest = await self._repository.get(action.scenario_id)
            if latest is None or latest.phase != "collecting":
                return self._refresh()
            recovery_session = await self._slot_recovery(latest)
        completion = await self._repository.complete_action(
            action.id,
            owner.channel,
            owner.chat_id,
            owner.customer_id,
            reply.to_result(),
            "booking_callback_completed",
            {
                "status": result.status,
                "error_code": result.error_code,
            },
            recovery_session=recovery_session,
        )
        return WorkflowReply.from_result(completion.result)

    @staticmethod
    def _is_confirm_slot_recovery(
        session: WorkflowSession | None,
        action: BookingAction,
    ) -> bool:
        if (
            session is None
            or action.action_kind != "confirm"
            or session.id != action.scenario_id
            or session.revision != action.revision
            or session.phase != "collecting"
            or session.state.get("step") != "awaiting_confirmation"
            or not isinstance(session.state.get("slot_query"), Mapping)
        ):
            return False
        selected_slot_id = session.state.get("selected_slot_id")
        return isinstance(selected_slot_id, str) and bool(selected_slot_id)

    async def _slot_recovery(
        self,
        session: WorkflowSession,
    ) -> WorkflowSession:
        state = self._state(session)
        for key in (
            "selected_date",
            "selected_slot_id",
            "actual_staff_id",
            "actual_staff_name",
            "starts_at",
            "selected_new_starts_at",
            "duration_minutes",
            "customer_name",
            "customer_phone",
            "personal_data_processing_allowed",
        ):
            if session.kind != "create" and key == "starts_at":
                continue
            state.pop(key, None)
        slots: list[Slot] = []
        raw_query = state.get("slot_query")
        if isinstance(raw_query, Mapping):
            now = self._aware_now()
            query = SlotQuery(
                service_ids=tuple(raw_query.get("service_ids", ())),
                starts_after=now,
                starts_before=now + self._horizon,
                staff_id=(
                    str(raw_query["staff_id"])
                    if raw_query.get("staff_id") is not None
                    else None
                ),
            )
            try:
                fresh = await self._booking_port.list_slots(query)
            except BookingTemporaryError:
                fresh = []
            staff_ids = {
                str(item["id"])
                for item in state.get("staff", ())
                if isinstance(item, Mapping) and item.get("id") is not None
            }
            slots = self._allowed_slots(fresh, query, staff_ids)
            state["slot_query"] = {
                "service_ids": list(query.service_ids),
                "starts_after": query.starts_after.isoformat(),
                "starts_before": query.starts_before.isoformat(),
                "staff_id": query.staff_id,
            }
        if slots:
            state["slots"] = [self._slot_state(slot) for slot in slots]
            state["date_page"] = 0
            state["step"] = "date"
        else:
            state.pop("slots", None)
            state["step"] = "master"
        return replace(
            session,
            phase="collecting",
            state=state,
            error_code=None,
            expires_at=None,
        )

    async def _render_current(self, session: WorkflowSession) -> WorkflowReply:
        if session.phase == "executing":
            return self._presenter.plain(
                "Запись уже завершается. Назад вернуться нельзя; дождитесь результата."
            )
        if session.phase == "awaiting_confirmation":
            return await self._render_summary(session)
        step = session.state.get("step")
        if step == "services":
            return await self._render_services(session)
        if step == "master":
            return await self._render_master(session)
        if step == "date":
            return await self._render_dates(session)
        if step == "slot":
            return await self._render_slots(session)
        if step == "customer_name":
            return await self._render_name(session, "Как вас зовут?")
        if step == "customer_contact":
            return self._presenter.request_contact(
                "Отправьте свой контакт кнопкой ниже. Чужой контакт не принимается."
            )
        if step == "booking":
            return await self._render_booking_choices(session)
        return self._refresh()

    async def _render_booking_choices(
        self,
        session: WorkflowSession,
    ) -> WorkflowReply:
        bookings = session.state.get("owned_bookings", ())
        specs = [
            (
                self._booking_label(item),
                "select_booking",
                {"booking_index": index},
            )
            for index, item in enumerate(bookings)
        ]
        specs.append(("Отмена", "cancel", {}))
        return await self._choice(session, "Выберите запись:", specs)

    async def _render_services(self, session: WorkflowSession) -> WorkflowReply:
        state = session.state
        services = list(state.get("services", ()))
        page = int(state.get("service_page", 0))
        selected = set(state.get("selected_service_ids", ()))
        start = page * self._page_size
        specs: list[tuple[str, str, dict[str, object]]] = []
        for service in services[start : start + self._page_size]:
            service_id = _required_text(service.get("id"))
            title = _required_text(service.get("title"))
            label = f"✓ {title}" if service_id in selected else title
            specs.append((label, "select_service", {"service_id": service_id}))
        if page > 0:
            specs.append(("Назад", "service_page", {"page": page - 1}))
        if start + self._page_size < len(services):
            specs.append(("Вперёд", "service_page", {"page": page + 1}))
        specs.extend(
            (
                ("Готово", "services_done", {}),
                ("Отмена", "cancel", {}),
            )
        )
        return await self._choice(session, "Выберите услуги:", specs)

    async def _render_master(self, session: WorkflowSession) -> WorkflowReply:
        specs = [("Любой мастер", "select_master", {"staff_id": None})]
        specs.extend(
            (
                _required_text(member.get("name")),
                "select_master",
                {"staff_id": _required_text(member.get("id"))},
            )
            for member in session.state.get("staff", ())
        )
        specs.extend((("Назад", "back", {}), ("Отмена", "cancel", {})))
        return await self._choice(session, "Выберите мастера:", specs)

    async def _render_dates(self, session: WorkflowSession) -> WorkflowReply:
        dates = self._dates(session.state)
        page = int(session.state.get("date_page", 0))
        start = page * self._page_size
        specs = [
            (
                datetime.fromisoformat(date_key).strftime("%d.%m"),
                "select_date",
                {"date": date_key},
            )
            for date_key in dates[start : start + self._page_size]
        ]
        if page > 0:
            specs.append(("Назад по датам", "date_page", {"page": page - 1}))
        if start + self._page_size < len(dates):
            specs.append(("Вперёд по датам", "date_page", {"page": page + 1}))
        specs.extend((("Назад", "back", {}), ("Отмена", "cancel", {})))
        return await self._choice(session, "Выберите дату:", specs)

    async def _render_slots(self, session: WorkflowSession) -> WorkflowReply:
        slots = self._slots_for_date(session.state)
        page = int(session.state.get("slot_page", 0))
        start = page * self._page_size
        staff = {
            item["id"]: item["name"] for item in session.state.get("staff", ())
        }
        specs = []
        for slot in slots[start : start + self._page_size]:
            local = datetime.fromisoformat(slot["starts_at"]).astimezone(
                self._timezone
            )
            label = f"{local:%H:%M} — {staff[slot['staff_id']]}"
            specs.append((label, "select_slot", {"slot_id": slot["id"]}))
        if page > 0:
            specs.append(("Назад по времени", "slot_page", {"page": page - 1}))
        if start + self._page_size < len(slots):
            specs.append(("Вперёд по времени", "slot_page", {"page": page + 1}))
        specs.extend((("Назад", "back", {}), ("Отмена", "cancel", {})))
        return await self._choice(session, "Выберите время:", specs)

    async def _render_name(
        self,
        session: WorkflowSession,
        text: str,
    ) -> WorkflowReply:
        return await self._choice(
            session,
            text,
            (("Назад", "back", {}), ("Отмена", "cancel", {})),
        )

    async def _render_summary(self, session: WorkflowSession) -> WorkflowReply:
        if session.kind != "create":
            return await self._render_change_summary(session)
        state = session.state
        services_by_id = {
            item["id"]: item["title"] for item in state.get("services", ())
        }
        titles = [
            services_by_id[service_id]
            for service_id in state.get("selected_service_ids", ())
        ]
        starts_at = datetime.fromisoformat(_required_text(state.get("starts_at")))
        local = starts_at.astimezone(self._timezone)
        phone = self._mask_phone(_required_text(state.get("customer_phone")))
        text = (
            "Проверьте запись:\n"
            f"Услуги: {', '.join(titles)}\n"
            f"Мастер: {_required_text(state.get('actual_staff_name'))}\n"
            f"Дата и время: {local:%d.%m.%Y %H:%M}\n"
            f"Имя: {_required_text(state.get('customer_name'))}\n"
            f"Телефон: {phone}"
        )
        return await self._choice(
            session,
            text,
            (
                ("Подтвердить", "confirm", {}),
                ("Изменить", "change", {}),
                ("Отмена", "cancel", {}),
            ),
            expires_at=session.expires_at,
        )

    async def _render_change_summary(
        self,
        session: WorkflowSession,
    ) -> WorkflowReply:
        state = session.state
        titles = [
            _required_text(item.get("title"))
            for item in state.get("services", ())
        ]
        old_local = datetime.fromisoformat(
            _required_text(state.get("starts_at"))
        ).astimezone(self._timezone)
        if session.kind == "cancel":
            text = (
                "Проверьте отмену записи:\n"
                f"Услуги: {', '.join(titles)}\n"
                f"Мастер: {_required_text(state.get('old_staff_name'))}\n"
                f"Дата и время: {old_local:%d.%m.%Y %H:%M}"
            )
            specs = (
                ("Да, отменить запись", "confirm", {}),
                ("Назад", "back", {}),
                ("Отмена", "cancel", {}),
            )
        else:
            new_local = datetime.fromisoformat(
                _required_text(state.get("selected_new_starts_at"))
            ).astimezone(self._timezone)
            text = (
                "Проверьте перенос записи:\n"
                f"Услуги: {', '.join(titles)}\n"
                f"Мастер: {_required_text(state.get('actual_staff_name'))}\n"
                f"Дата и время: {old_local:%d.%m.%Y %H:%M} → "
                f"{new_local:%d.%m.%Y %H:%M}"
            )
            specs = (
                ("Подтвердить", "confirm", {}),
                ("Назад", "back", {}),
                ("Отмена", "cancel", {}),
            )
        return await self._choice(
            session,
            text,
            specs,
            expires_at=session.expires_at,
        )

    async def _choice(
        self,
        session: WorkflowSession,
        text: str,
        specs,
        *,
        expires_at: datetime | None = None,
    ) -> WorkflowReply:
        expiry = expires_at or (self._aware_now() + self._confirmation_ttl)
        actions = []
        for label, kind, payload in specs:
            action = await self._repository.issue_action(
                session.id,
                session.revision,
                kind,
                payload,
                expiry,
            )
            actions.append(PresentedAction(label, action.id))
        return self._presenter.choice(text, actions)

    def _allowed_slots(
        self,
        slots: list[Slot],
        query: SlotQuery,
        staff_ids: set[str],
    ) -> list[Slot]:
        selected = set(query.service_ids)
        return sorted(
            (
                slot
                for slot in slots
                if selected.issubset(slot.service_ids)
                and slot.staff_id in staff_ids
                and (
                    query.staff_id is None or slot.staff_id == query.staff_id
                )
                and query.starts_after <= slot.starts_at < query.starts_before
            ),
            key=lambda slot: (slot.starts_at, slot.staff_id, slot.id),
        )

    async def _load_protected_bookings(
        self,
        customer_id: str,
    ) -> list[dict[str, object]] | None:
        try:
            local_bookings = await self._repository.list_owned_active_bookings(
                customer_id
            )
            services = await self._catalog.list_services()
            services_by_id = {service.id: service for service in services}
            protected_bookings = []
            for local in local_bookings:
                protected = await self._booking_port.get_booking(
                    GetBooking(
                        local.external_id,
                        local.customer_id,
                        local.booking_key,
                    )
                )
                if not booking_snapshots_match(local, protected):
                    return None
                selected_services = []
                for service_id in local.service_ids:
                    service = services_by_id.get(service_id)
                    if service is None:
                        return None
                    selected_services.append(
                        {
                            "id": service.id,
                            "title": service.title,
                            "duration_minutes": service.duration_minutes,
                        }
                    )
                staff = await self._catalog.list_staff(local.service_ids)
                member = next(
                    (item for item in staff if item.id == local.staff_id),
                    None,
                )
                if member is None:
                    return None
                protected_bookings.append(
                    self._owned_booking_state(local, selected_services, member.name)
                )
            return protected_bookings
        except (
            BookingNotFound,
            BookingTemporaryError,
            BookingOutcomeUnknown,
            TimeoutError,
            ValueError,
        ):
            return None

    @staticmethod
    def _owned_booking_state(
        booking: ExternalBooking,
        services: list[dict[str, object]],
        staff_name: str,
    ) -> dict[str, object]:
        return {
            "external_id": booking.external_id,
            "customer_id": booking.customer_id,
            "booking_key": str(booking.booking_key),
            "slot_id": booking.slot_id,
            "service_ids": list(booking.service_ids),
            "services": services,
            "staff_id": booking.staff_id,
            "staff_name": staff_name,
            "starts_at": booking.starts_at.isoformat(),
            "scheduled_end_at": (
                booking.scheduled_end_at.isoformat()
                if booking.scheduled_end_at is not None
                else None
            ),
        }

    def _booking_label(self, booking: Mapping[str, object]) -> str:
        starts_at = datetime.fromisoformat(
            _required_text(booking.get("starts_at"))
        ).astimezone(self._timezone)
        services = ", ".join(
            _required_text(item.get("title"))
            for item in booking.get("services", ())
        )
        return f"{starts_at:%d.%m.%Y %H:%M} — {services}"

    def _booking_summary(
        self,
        booking: Mapping[str, object],
        number: int,
    ) -> str:
        return (
            f"{number}. {self._booking_label(booking)}\n"
            f"Мастер: {_required_text(booking.get('staff_name'))}"
        )

    @staticmethod
    def _slot_state(slot: Slot) -> dict[str, object]:
        return {
            "id": slot.id,
            "service_ids": list(slot.service_ids),
            "staff_id": slot.staff_id,
            "starts_at": slot.starts_at.isoformat(),
            "duration_minutes": slot.duration_minutes,
        }

    def _dates(self, state: Mapping[str, object]) -> list[str]:
        return sorted(
            {
                datetime.fromisoformat(slot["starts_at"])
                .astimezone(self._timezone)
                .date()
                .isoformat()
                for slot in state.get("slots", ())
            }
        )

    def _slots_for_date(self, state: Mapping[str, object]) -> list[dict]:
        selected_date = state.get("selected_date")
        slots = [
            dict(_plain(slot))
            for slot in state.get("slots", ())
            if datetime.fromisoformat(slot["starts_at"])
            .astimezone(self._timezone)
            .date()
            .isoformat()
            == selected_date
        ]
        return sorted(
            slots,
            key=lambda slot: (
                slot["starts_at"],
                slot["staff_id"],
                slot["id"],
            ),
        )

    @staticmethod
    def _state(session: WorkflowSession) -> dict[str, object]:
        plain = _plain(session.state)
        if not isinstance(plain, dict):
            raise ValueError("workflow state must be an object")
        return plain

    def _aware_now(self) -> datetime:
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("workflow clock must be timezone-aware")
        return now

    @staticmethod
    def _normalize_phone(value: str) -> str | None:
        stripped = value.strip()
        prefix = "+" if stripped.startswith("+") else ""
        digits = "".join(character for character in stripped if character.isdigit())
        normalized = prefix + digits
        if not prefix or not 10 <= len(digits) <= 15:
            return None
        return normalized

    @staticmethod
    def _mask_phone(value: str) -> str:
        if len(value) < 7:
            raise ValueError("phone cannot be masked safely")
        return f"{value[:2]}{'*' * (len(value) - 6)}{value[-4:]}"

    def _refresh(self) -> WorkflowReply:
        return self._presenter.plain(
            "Срок действия кнопки истёк или данные обновились. Откройте запись заново."
        )

    def _unavailable(self) -> WorkflowReply:
        return self._presenter.plain(
            "Сервис записи временно недоступен. Попробуйте позже."
        )
