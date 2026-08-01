import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import UUID

import pytest

from moroz.booking.catalog import CatalogService, CatalogStaff
from moroz.booking.interaction import BookingOwner, Interaction, WorkflowReply
from moroz.booking.models import BookingTemporaryError, Slot, SlotQuery
from moroz.booking.workflow import BookingWorkflow
from moroz.booking.workflow_repository import (
    ActionCompletion,
    BookingAction,
    WorkflowSession,
)
from moroz.messaging.models import ScenarioResult


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class FakeCatalog:
    def __init__(self, services=None, staff=None) -> None:
        self.services = list(
            services
            if services is not None
            else (
                CatalogService("service-331", "Крио", 30),
                CatalogService("service-442", "Массаж", 60),
                CatalogService("service-553", "Солярий", 20),
            )
        )
        self.staff = list(
            staff
            if staff is not None
            else (
                CatalogStaff(
                    "staff-6544",
                    "Анна",
                    ("service-331", "service-442", "service-553"),
                ),
            )
        )
        self.services_error: Exception | None = None
        self.staff_error: Exception | None = None
        self.staff_queries: list[tuple[str, ...]] = []

    async def list_services(self):
        if self.services_error:
            raise self.services_error
        return list(self.services)

    async def list_staff(self, service_ids):
        if self.staff_error:
            raise self.staff_error
        self.staff_queries.append(service_ids)
        selected = set(service_ids)
        return [item for item in self.staff if selected.issubset(item.service_ids)]


class FakePort:
    def __init__(self, slots=None) -> None:
        self.slots = list(
            slots
            if slots is not None
            else (
                Slot(
                    "slot-provider-991",
                    ("service-331",),
                    "staff-6544",
                    datetime(2026, 8, 2, 7, 0, tzinfo=UTC),
                    30,
                ),
                Slot(
                    "slot-provider-992",
                    ("service-331",),
                    "staff-6544",
                    datetime(2026, 8, 3, 8, 0, tzinfo=UTC),
                    30,
                ),
            )
        )
        self.error: Exception | None = None
        self.queries: list[SlotQuery] = []

    async def list_slots(self, query: SlotQuery):
        self.queries.append(query)
        if self.error:
            raise self.error
        return list(self.slots)


class FakeWorkflowRepository:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.session: WorkflowSession | None = None
        self.actions: dict[str, BookingAction] = {}
        self.events: list[tuple[str, dict[str, object]]] = []
        self._next_action = 1

    async def start(self, kind, channel, chat_id, customer_id, idempotency_key):
        if self.session is None or self.session.phase not in {
            "collecting",
            "awaiting_confirmation",
            "executing",
        }:
            now = self.clock.now()
            self.session = WorkflowSession(
                UUID("00000000-0000-0000-0000-000000000006"),
                kind,
                "collecting",
                idempotency_key,
                customer_id,
                channel,
                chat_id,
                0,
                {},
                None,
                None,
                now,
                now,
            )
        return self.session

    async def get_active(self, channel, chat_id, customer_id):
        session = self.session
        if session is None:
            return None
        if (
            (session.channel, session.chat_id, session.customer_id)
            != (channel, chat_id, customer_id)
            or session.phase not in {
                "collecting",
                "awaiting_confirmation",
                "executing",
            }
        ):
            return None
        return session

    async def checkpoint(
        self,
        session,
        event_type,
        payload=None,
        *,
        action_id=None,
        result=None,
    ):
        assert self.session is not None
        if self.session.revision != session.revision:
            raise RuntimeError("workflow revision conflict")
        now = self.clock.now()
        self.session = replace(session, revision=session.revision + 1, updated_at=now)
        self.events.append((event_type, dict(payload or {})))
        if action_id is not None:
            action = self.actions[action_id]
            self.actions[action_id] = replace(
                action,
                consumed_at=now,
                result=MappingProxyType(dict(result)),
            )
        return self.session

    async def issue_action(
        self,
        scenario_id,
        revision,
        action_kind,
        payload,
        expires_at,
    ):
        assert self.session is not None
        assert revision == self.session.revision
        action_id = f"a{self._next_action}"
        self._next_action += 1
        action = BookingAction(
            action_id,
            scenario_id,
            self.session.customer_id,
            self.session.channel,
            self.session.chat_id,
            revision,
            action_kind,
            payload,
            expires_at,
            None,
            None,
        )
        self.actions[action_id] = action
        return action

    async def consume_action(self, action_id, channel, chat_id, customer_id):
        action = self.actions.get(action_id)
        if action is None or self.session is None:
            return None
        if (action.channel, action.chat_id, action.customer_id) != (
            channel,
            chat_id,
            customer_id,
        ):
            return None
        if action.consumed_at is not None and action.result is not None:
            return action
        if (
            action.expires_at <= self.clock.now()
            or action.revision != self.session.revision
        ):
            return None
        return action

    async def complete_action(
        self,
        action_id,
        channel,
        chat_id,
        customer_id,
        result,
        event_type,
        payload=None,
    ):
        assert self.session is not None
        action = self.actions[action_id]
        if action.consumed_at is not None:
            if action.result != result:
                raise RuntimeError("booking action result conflict")
            return ActionCompletion(self.session, action.result, True)
        assert (action.channel, action.chat_id, action.customer_id) == (
            channel,
            chat_id,
            customer_id,
        )
        now = self.clock.now()
        self.session = replace(
            self.session,
            revision=self.session.revision + 1,
            updated_at=now,
        )
        saved = MappingProxyType(dict(result))
        self.actions[action_id] = replace(
            action,
            consumed_at=now,
            result=saved,
        )
        self.events.append((event_type, dict(payload or {})))
        return ActionCompletion(self.session, saved, False)


class FakeBookingService:
    def __init__(self, repository: FakeWorkflowRepository) -> None:
        self.repository = repository
        self.calls: list[tuple[UUID, bool]] = []
        self.result = ScenarioResult("ok", "unsafe provider reply", None, ())

    async def handle(self, scenario_id, *, confirmed):
        self.calls.append((scenario_id, confirmed))
        if self.repository.session is not None:
            phase = "confirmed" if self.result.status == "ok" else "escalated"
            self.repository.session = replace(self.repository.session, phase=phase)
        return self.result


@pytest.fixture
def owner():
    return BookingOwner("telegram", "chat-10", "user-10")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def dependencies(clock):
    repository = FakeWorkflowRepository(clock)
    catalog = FakeCatalog()
    port = FakePort()
    service = FakeBookingService(repository)
    return repository, catalog, port, service


@pytest.fixture
def workflow(clock, dependencies):
    repository, catalog, port, service = dependencies
    return BookingWorkflow(
        catalog,
        port,
        repository,
        service,
        now=clock.now,
        page_size=2,
    )


def _buttons(reply: WorkflowReply) -> list[dict[str, object]]:
    return [
        button
        for row in reply.delivery_options.get("reply_markup", {}).get(
            "inline_keyboard", []
        )
        for button in row
    ]


def _button_texts(reply: WorkflowReply) -> list[str]:
    return [str(button["text"]) for button in _buttons(reply)]


def _callback(reply: WorkflowReply, text: str) -> str:
    return next(
        str(button["callback_data"])
        for button in _buttons(reply)
        if button["text"] == text
    )


async def _press(workflow, owner, reply, text, key="callback"):
    return await workflow.handle(
        Interaction.callback(owner, f"{key}:{text}", _callback(reply, text))
    )


async def _ready_confirmation(workflow, owner):
    reply = await workflow.start_create(owner, "command:10")
    reply = await _press(workflow, owner, reply, "Крио")
    reply = await _press(workflow, owner, reply, "Готово")
    reply = await _press(workflow, owner, reply, "Любой мастер")
    date_text = _button_texts(reply)[0]
    reply = await _press(workflow, owner, reply, date_text)
    slot_text = _button_texts(reply)[0]
    reply = await _press(workflow, owner, reply, slot_text)
    reply = await workflow.handle(
        Interaction.text(owner, "name:10", "Мария")
    )
    assert reply.delivery_options["reply_markup"]["keyboard"][0][0][
        "request_contact"
    ] is True
    return await workflow.handle(
        Interaction.contact(
            owner,
            "contact:10",
            contact_user_id="user-10",
            phone_number="+7 000 000-00-00",
            personal_data_processing_allowed=True,
        )
    )


@pytest.mark.asyncio
async def test_full_create_flow_is_owned_14_day_aware_and_explicit(
    workflow,
    owner,
    dependencies,
):
    repository, catalog, port, service = dependencies

    reply = await workflow.start_create(owner, "command:10")
    assert _button_texts(reply) == ["Крио", "Массаж", "Вперёд", "Готово", "Отмена"]
    reply = await _press(workflow, owner, reply, "Крио")
    reply = await _press(workflow, owner, reply, "Готово")
    assert _button_texts(reply) == ["Любой мастер", "Анна", "Назад", "Отмена"]

    reply = await _press(workflow, owner, reply, "Любой мастер")
    query = port.queries[-1]
    assert query.starts_after == datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    assert query.starts_before == query.starts_after + timedelta(days=14)
    assert query.staff_id is None
    assert query.service_ids == ("service-331",)

    reply = await _press(workflow, owner, reply, _button_texts(reply)[0])
    reply = await _press(workflow, owner, reply, _button_texts(reply)[0])
    reply = await workflow.handle(Interaction.text(owner, "name:10", "Мария"))
    assert reply.delivery_options["reply_markup"]["keyboard"][0][0][
        "request_contact"
    ] is True
    summary = await workflow.handle(
        Interaction.contact(
            owner,
            "contact:10",
            contact_user_id="user-10",
            phone_number="+7 000 000-00-00",
            personal_data_processing_allowed=True,
        )
    )

    assert "Анна" in summary.text
    assert "Любой мастер" not in summary.text
    assert "+7******0000" in summary.text
    assert "+70000000000" not in summary.text
    assert _button_texts(summary) == ["Подтвердить", "Изменить", "Отмена"]
    assert repository.session.expires_at == datetime(
        2026, 8, 1, 9, 30, tzinfo=UTC
    )

    confirmed = await _press(workflow, owner, summary, "Подтвердить")

    assert confirmed.text == "Запись подтверждена."
    assert service.calls == [(repository.session.id, True)]
    assert repository.session.phase == "confirmed"
    assert repository.session.state["personal_data_processing_allowed"] is True


@pytest.mark.asyncio
async def test_callbacks_are_opaque_and_contain_no_provider_ids_or_phone(
    workflow,
    owner,
    dependencies,
):
    repository, _catalog, _port, _service = dependencies
    summary = await _ready_confirmation(workflow, owner)

    rendered = json.dumps(summary.to_result(), ensure_ascii=False)
    callbacks = [str(button["callback_data"]) for button in _buttons(summary)]
    action_payloads = [dict(action.payload) for action in repository.actions.values()]

    assert callbacks
    assert all(re.fullmatch(r"booking:a\d+", item) for item in callbacks)
    for forbidden in (
        "service-331",
        "staff-6544",
        "slot-provider-991",
        "+70000000000",
    ):
        assert forbidden not in rendered
    assert "+70000000000" not in json.dumps(action_payloads, ensure_ascii=False)


@pytest.mark.asyncio
async def test_services_are_paginated_and_multi_select_has_no_count_cap(
    workflow,
    owner,
    dependencies,
):
    _repository, catalog, _port, _service = dependencies
    reply = await workflow.start_create(owner, "command:pages")
    assert _button_texts(reply) == ["Крио", "Массаж", "Вперёд", "Готово", "Отмена"]

    reply = await _press(workflow, owner, reply, "Крио")
    reply = await _press(workflow, owner, reply, "Вперёд")
    assert "Солярий" in _button_texts(reply)
    reply = await _press(workflow, owner, reply, "Солярий")
    reply = await _press(workflow, owner, reply, "Назад")
    reply = await _press(workflow, owner, reply, "Массаж")
    reply = await _press(workflow, owner, reply, "Готово")

    assert catalog.staff_queries[-1] == (
        "service-331",
        "service-442",
        "service-553",
    )


@pytest.mark.asyncio
async def test_dates_and_slots_are_paginated_deterministically(
    workflow,
    owner,
    dependencies,
):
    _repository, _catalog, port, _service = dependencies
    port.slots = [
        Slot(
            f"slot-{day}-{hour}",
            ("service-331",),
            "staff-6544",
            datetime(2026, 8, day, hour, tzinfo=UTC),
            30,
        )
        for day, hour in ((2, 7), (3, 7), (4, 7), (4, 8), (4, 9))
    ]
    reply = await workflow.start_create(owner, "command:date-pages")
    reply = await _press(workflow, owner, reply, "Крио")
    reply = await _press(workflow, owner, reply, "Готово")
    reply = await _press(workflow, owner, reply, "Любой мастер")

    assert "Вперёд по датам" in _button_texts(reply)
    reply = await _press(workflow, owner, reply, "Вперёд по датам")
    assert _button_texts(reply)[0] == "04.08"
    reply = await _press(workflow, owner, reply, "04.08")
    assert _button_texts(reply)[:2] == ["10:00 — Анна", "11:00 — Анна"]
    assert "Вперёд по времени" in _button_texts(reply)
    reply = await _press(workflow, owner, reply, "Вперёд по времени")
    assert _button_texts(reply)[0] == "12:00 — Анна"


@pytest.mark.asyncio
async def test_done_without_services_and_incompatible_combination_fail_closed(
    workflow,
    owner,
    dependencies,
):
    repository, catalog, _port, service = dependencies
    reply = await workflow.start_create(owner, "command:none")
    revision = repository.session.revision

    empty = await _press(workflow, owner, reply, "Готово")
    assert "хотя бы одну" in empty.text.casefold()
    assert repository.session.revision == revision

    selected = await _press(workflow, owner, reply, "Крио")
    catalog.staff = []
    incompatible = await _press(workflow, owner, selected, "Готово")
    assert "недоступ" in incompatible.text.casefold()
    assert service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "services_empty",
        "services_temporary",
        "staff_empty",
        "staff_temporary",
        "slots_empty",
        "slots_temporary",
    ],
)
async def test_empty_or_temporary_provider_reads_fail_closed(
    workflow,
    owner,
    dependencies,
    stage,
):
    repository, catalog, port, service = dependencies
    if stage.startswith("services"):
        if stage.endswith("empty"):
            catalog.services = []
        else:
            catalog.services_error = BookingTemporaryError()
        reply = await workflow.start_create(owner, "command:temporary")
    else:
        reply = await workflow.start_create(owner, "command:temporary")
        reply = await _press(workflow, owner, reply, "Крио")
        if stage.startswith("staff"):
            if stage.endswith("empty"):
                catalog.staff = []
            else:
                catalog.staff_error = BookingTemporaryError()
            reply = await _press(workflow, owner, reply, "Готово")
        else:
            reply = await _press(workflow, owner, reply, "Готово")
            if stage.endswith("empty"):
                port.slots = []
            else:
                port.error = BookingTemporaryError()
            reply = await _press(workflow, owner, reply, "Любой мастер")

    assert "недоступ" in reply.text.casefold()
    assert service.calls == []
    assert all("phone" not in json.dumps(payload) for _, payload in repository.events)


@pytest.mark.asyncio
async def test_foreign_contact_and_foreign_action_never_change_state(
    workflow,
    owner,
    dependencies,
):
    repository, _catalog, _port, service = dependencies
    summary = await _ready_confirmation(workflow, owner)
    before = repository.session

    foreign_action = await workflow.handle(
        Interaction.callback(
            BookingOwner("telegram", "chat-11", "user-11"),
            "foreign:1",
            _callback(summary, "Подтвердить"),
        )
    )
    assert "истёк" in foreign_action.text.casefold()
    assert repository.session == before
    assert service.calls == []

    changed = await _press(workflow, owner, summary, "Изменить")
    changed = await _press(workflow, owner, changed, "Крио")
    changed = await _press(workflow, owner, changed, "Готово")
    changed = await _press(workflow, owner, changed, "Любой мастер")
    changed = await _press(workflow, owner, changed, _button_texts(changed)[0])
    changed = await _press(workflow, owner, changed, _button_texts(changed)[0])
    await workflow.handle(Interaction.text(owner, "name:foreign", "Мария"))
    before = repository.session
    denied = await workflow.handle(
        Interaction.contact(
            owner,
            "contact:foreign",
            contact_user_id="user-11",
            phone_number="+79991234567",
            personal_data_processing_allowed=True,
        )
    )
    assert "свой контакт" in denied.text.casefold()
    assert repository.session == before
    assert "customer_phone" not in repository.session.state


@pytest.mark.asyncio
async def test_contact_without_processing_consent_never_persists_phone(
    workflow,
    owner,
    dependencies,
):
    repository, _catalog, _port, service = dependencies
    reply = await workflow.start_create(owner, "command:no-consent")
    reply = await _press(workflow, owner, reply, "Крио")
    reply = await _press(workflow, owner, reply, "Готово")
    reply = await _press(workflow, owner, reply, "Любой мастер")
    reply = await _press(workflow, owner, reply, _button_texts(reply)[0])
    reply = await _press(workflow, owner, reply, _button_texts(reply)[0])
    await workflow.handle(Interaction.text(owner, "name:no-consent", "Мария"))
    before = repository.session

    denied = await workflow.handle(
        Interaction.contact(
            owner,
            "contact:no-consent",
            contact_user_id="user-10",
            phone_number="+79991234567",
            personal_data_processing_allowed=False,
        )
    )

    assert "соглас" in denied.text.casefold()
    assert repository.session == before
    assert "customer_phone" not in repository.session.state
    assert service.calls == []


@pytest.mark.asyncio
async def test_back_invalidates_old_callbacks_and_executing_has_no_back_path(
    workflow,
    owner,
    dependencies,
):
    repository, _catalog, _port, service = dependencies
    services = await workflow.start_create(owner, "command:back")
    selected = await _press(workflow, owner, services, "Крио")
    master = await _press(workflow, owner, selected, "Готово")
    old_master_callback = _callback(master, "Любой мастер")
    services_again = await _press(workflow, owner, master, "Назад")
    before = repository.session

    stale = await workflow.handle(
        Interaction.callback(owner, "stale:1", old_master_callback)
    )
    assert "истёк" in stale.text.casefold()
    assert repository.session == before

    repository.session = replace(repository.session, phase="executing")
    no_back = await workflow.handle(Interaction.text(owner, "back:executing", "Назад"))
    assert "заверша" in no_back.text.casefold()
    assert "Назад" not in _button_texts(no_back)
    assert service.calls == []
    assert any(text.endswith("Крио") for text in _button_texts(services_again))


@pytest.mark.asyncio
async def test_expired_confirmation_and_consumed_replay_call_service_once(
    workflow,
    owner,
    dependencies,
    clock,
):
    repository, _catalog, _port, service = dependencies
    summary = await _ready_confirmation(workflow, owner)
    confirm_callback = _callback(summary, "Подтвердить")
    clock.advance(minutes=31)

    expired = await workflow.handle(
        Interaction.callback(owner, "confirm:expired", confirm_callback)
    )
    assert "истёк" in expired.text.casefold()
    assert service.calls == []

    clock.value -= timedelta(minutes=31)
    action = repository.actions[confirm_callback.removeprefix("booking:")]
    repository.actions[action.id] = replace(
        action,
        expires_at=clock.now() + timedelta(minutes=30),
    )
    first = await workflow.handle(
        Interaction.callback(owner, "confirm:first", confirm_callback)
    )
    second = await workflow.handle(
        Interaction.callback(owner, "confirm:second", confirm_callback)
    )

    assert first == second
    assert len(service.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            ScenarioResult(
                "needs_input",
                "unsafe provider",
                "choose_slot",
                (),
            ),
            "уже недоступно",
        ),
        (
            ScenarioResult(
                "escalated",
                "unsafe provider",
                None,
                (),
                "booking_outcome_unknown",
            ),
            "не обещаем слот",
        ),
        (
            ScenarioResult(
                "escalated",
                "unsafe provider",
                None,
                (),
                "booking_temporarily_unavailable",
            ),
            "не обещаем слот",
        ),
    ],
)
async def test_confirmation_failures_never_promise_a_booking(
    workflow,
    owner,
    dependencies,
    result,
    expected,
):
    _repository, _catalog, _port, service = dependencies
    summary = await _ready_confirmation(workflow, owner)
    service.result = result

    reply = await _press(workflow, owner, summary, "Подтвердить")

    assert expected in reply.text.casefold()
    assert "запись подтверждена" not in reply.text.casefold()


@pytest.mark.asyncio
async def test_raw_pii_is_only_in_workflow_state_not_reply_action_or_audit(
    workflow,
    owner,
    dependencies,
):
    repository, _catalog, _port, _service = dependencies
    raw_phone = "+70000000000"
    summary = await _ready_confirmation(workflow, owner)

    assert repository.session.state["customer_phone"] == raw_phone
    assert raw_phone not in json.dumps(summary.to_result(), ensure_ascii=False)
    assert raw_phone not in json.dumps(
        [dict(action.payload) for action in repository.actions.values()],
        ensure_ascii=False,
    )
    assert raw_phone not in json.dumps(repository.events, ensure_ascii=False)
