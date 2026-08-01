import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from moroz.booking.interaction import BookingOwner, Interaction
from moroz.booking.models import (
    BookingIdentity,
    BookingNotFound,
    ExternalBooking,
    Slot,
)
from moroz.booking.workflow import BookingWorkflow
from moroz.messaging.models import ScenarioResult
from tests.unit.booking.test_workflow_create import (
    Clock,
    FakeCatalog,
    FakePort,
    FakeWorkflowRepository,
    _button_texts,
    _callback,
    _press,
)


OWNER = BookingOwner("telegram", "user-10", "user-10")
OLD_START = datetime(2026, 8, 2, 9, tzinfo=UTC)
NEW_START = datetime(2026, 8, 3, 10, tzinfo=UTC)
BOOKING_KEY = UUID("00000000-0000-0000-0000-000000000888")


def _booking(**changes) -> ExternalBooking:
    booking = ExternalBooking(
        external_id="provider-secret-42",
        customer_id="user-10",
        booking_key=BOOKING_KEY,
        slot_id="slot-old-secret",
        service_ids=("service-331", "service-442"),
        staff_id="staff-6544",
        starts_at=OLD_START,
        status="confirmed",
        scheduled_end_at=OLD_START + timedelta(minutes=90),
    )
    return replace(booking, **changes)


class ChangeRepository(FakeWorkflowRepository):
    def __init__(self, clock: Clock) -> None:
        super().__init__(clock)
        self.owned = [_booking()]

    async def list_owned_active_bookings(self, customer_id):
        return [item for item in self.owned if item.customer_id == customer_id]


class ChangePort(FakePort):
    def __init__(self) -> None:
        super().__init__(
            [
                Slot(
                    "slot-new-secret",
                    ("service-331", "service-442"),
                    "staff-6544",
                    NEW_START,
                    90,
                )
            ]
        )
        self.provider = _booking()
        self.providers = {}
        self.get_calls = []
        self.get_error = None

    async def get_booking(self, command):
        self.get_calls.append(command)
        if self.get_error is not None:
            raise self.get_error
        return self.providers.get(command.external_id, self.provider)


class ChangeService:
    def __init__(self, repository: ChangeRepository) -> None:
        self.repository = repository
        self.handle_calls = []
        self.escalate_calls = []
        self.result = ScenarioResult("ok", "provider detail", None, ())

    async def handle(self, scenario_id, *, confirmed, identity=None):
        self.handle_calls.append((scenario_id, confirmed, identity))
        phase = "confirmed" if self.result.status == "ok" else "collecting"
        self.repository.session = replace(self.repository.session, phase=phase)
        return self.result

    async def escalate(self, scenario_id, *, identity, error_code):
        self.escalate_calls.append((scenario_id, identity, error_code))
        self.repository.session = replace(
            self.repository.session,
            phase="escalated",
            error_code=error_code,
        )
        return ScenarioResult(
            "escalated",
            "provider detail",
            None,
            (),
            error_code,
        )


@pytest.fixture
def change_dependencies():
    clock = Clock()
    repository = ChangeRepository(clock)
    catalog = FakeCatalog()
    port = ChangePort()
    service = ChangeService(repository)
    workflow = BookingWorkflow(
        catalog,
        port,
        repository,
        service,
        now=clock.now,
    )
    return workflow, repository, catalog, port, service


@pytest.mark.asyncio
async def test_owned_list_uses_exact_protected_get_and_hides_provider_ids(
    change_dependencies,
):
    workflow, _repository, _catalog, port, _service = change_dependencies

    reply = await workflow.list_bookings(OWNER)

    assert all(value in reply.text for value in ("Крио", "Массаж", "Анна"))
    assert len(port.get_calls) == 1
    assert port.get_calls[0].external_id == "provider-secret-42"
    rendered = json.dumps(reply.to_result(), ensure_ascii=False)
    assert "provider-secret-42" not in rendered
    assert str(BOOKING_KEY) not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider",
    [
        _booking(customer_id="foreign"),
        _booking(slot_id="changed"),
        _booking(service_ids=("service-331",)),
        _booking(staff_id="changed"),
        _booking(starts_at=OLD_START + timedelta(minutes=1)),
        _booking(status="cancelled"),
    ],
)
async def test_protected_list_mismatch_is_generic_and_discloses_nothing(
    change_dependencies,
    provider,
):
    workflow, _repository, _catalog, port, _service = change_dependencies
    port.provider = provider

    reply = await workflow.list_bookings(OWNER)

    assert reply.text == "Не удалось безопасно проверить ваши записи. Попробуйте позже."
    assert "Крио" not in reply.text
    assert "foreign" not in reply.text


@pytest.mark.asyncio
async def test_protected_list_not_found_uses_same_generic_response(
    change_dependencies,
):
    workflow, _repository, _catalog, port, _service = change_dependencies
    port.get_error = BookingNotFound("secret")

    reply = await workflow.list_bookings(OWNER)

    assert reply.text == "Не удалось безопасно проверить ваши записи. Попробуйте позже."


@pytest.mark.asyncio
async def test_reschedule_preserves_services_and_old_start_until_confirmation(
    change_dependencies,
):
    workflow, repository, catalog, _port, service = change_dependencies
    reply = await workflow.start_reschedule(OWNER, "change:1")
    assert len(_button_texts(reply)) == 2
    assert "provider-secret-42" not in json.dumps(reply.to_result())

    reply = await _press(workflow, OWNER, reply, _button_texts(reply)[0])
    assert _button_texts(reply) == ["Любой мастер", "Анна", "Назад", "Отмена"]
    assert catalog.staff_queries[-1] == ("service-331", "service-442")
    assert repository.session.state["starts_at"] == OLD_START.isoformat()

    reply = await _press(workflow, OWNER, reply, "Любой мастер")
    reply = await _press(workflow, OWNER, reply, _button_texts(reply)[0])
    reply = await _press(workflow, OWNER, reply, _button_texts(reply)[0])

    assert all(value in reply.text for value in ("Крио", "Массаж", "Анна"))
    assert "12:00" in reply.text and "13:00" in reply.text
    assert repository.session.state["starts_at"] == OLD_START.isoformat()
    assert repository.session.state["selected_new_starts_at"] == NEW_START.isoformat()
    assert _button_texts(reply) == ["Подтвердить", "Назад", "Отмена"]

    confirmed = await _press(workflow, OWNER, reply, "Подтвердить")
    assert confirmed.text == "Запись перенесена."
    assert service.handle_calls == [
        (
            repository.session.id,
            True,
            BookingIdentity("user-10", True),
        )
    ]


@pytest.mark.asyncio
async def test_cancel_has_distinct_full_summary_and_exact_confirmation_label(
    change_dependencies,
):
    workflow, repository, _catalog, _port, service = change_dependencies
    reply = await workflow.start_cancel(OWNER, "cancel:1")
    reply = await _press(workflow, OWNER, reply, _button_texts(reply)[0])

    assert all(value in reply.text for value in ("Крио", "Массаж", "Анна", "12:00"))
    assert _button_texts(reply) == ["Да, отменить запись", "Назад", "Отмена"]
    assert repository.session.state["starts_at"] == OLD_START.isoformat()

    confirmed = await _press(workflow, OWNER, reply, "Да, отменить запись")
    assert confirmed.text == "Запись отменена."
    assert service.handle_calls == [
        (
            repository.session.id,
            True,
            BookingIdentity("user-10", True),
        )
    ]


@pytest.mark.asyncio
async def test_cancel_navigation_does_not_claim_provider_cancellation(
    change_dependencies,
):
    workflow, _repository, _catalog, _port, service = change_dependencies
    reply = await workflow.start_cancel(OWNER, "cancel:abort")

    aborted = await _press(workflow, OWNER, reply, "Отмена")

    assert aborted.text == "Действие отменено. Изменения не отправлялись."
    assert service.handle_calls == []


@pytest.mark.asyncio
async def test_partial_service_change_escalates_without_mutation(
    change_dependencies,
):
    workflow, repository, _catalog, _port, service = change_dependencies
    reply = await workflow.start_reschedule(OWNER, "change:partial")
    await _press(workflow, OWNER, reply, _button_texts(reply)[0])

    result = await workflow.handle(
        Interaction.text(OWNER, "partial:1", "Добавить услугу")
    )

    assert "администратор" in result.text.casefold()
    assert service.handle_calls == []
    assert service.escalate_calls == [
        (
            repository.session.id,
            BookingIdentity("user-10", True),
            "partial_service_change_unsupported",
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Хочу добавить услугу",
        "Можно убрать массаж?",
        "Поменяйте услугу",
        "Добавьте, пожалуйста, ещё одну услугу",
        "Я хочу изменить набор услуг",
        "Удалите массаж из записи",
        "Хочу временно изменить услугу",
        "Хочу изменить услугу и поменять время",
    ],
)
async def test_clear_inflected_partial_service_request_escalates(
    change_dependencies,
    text,
):
    workflow, repository, _catalog, _port, service = change_dependencies
    reply = await workflow.start_reschedule(OWNER, f"change:partial:{text}")
    await _press(workflow, OWNER, reply, _button_texts(reply)[0])

    result = await workflow.handle(Interaction.text(OWNER, "partial:inflected", text))

    assert "администратор" in result.text.casefold()
    assert service.handle_calls == []
    assert service.escalate_calls == [
        (
            repository.session.id,
            BookingIdentity("user-10", True),
            "partial_service_change_unsupported",
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Услугу хочу временно изменить",
        "Массаж хочу временно убрать",
    ],
)
@pytest.mark.parametrize("kind", ["reschedule", "cancel"])
async def test_temporary_adverb_does_not_hide_partial_service_target(
    change_dependencies,
    text,
    kind,
):
    workflow, repository, _catalog, _port, service = change_dependencies
    if kind == "reschedule":
        reply = await workflow.start_reschedule(
            OWNER,
            f"change:temporary:{text}",
        )
        await _press(workflow, OWNER, reply, _button_texts(reply)[0])
    else:
        reply = await workflow.start_cancel(OWNER, f"cancel:temporary:{text}")
        await _press(workflow, OWNER, reply, _button_texts(reply)[0])

    result = await workflow.handle(
        Interaction.text(OWNER, f"temporary:{kind}:{text}", text)
    )

    assert "администратор" in result.text.casefold()
    assert service.handle_calls == []
    assert service.escalate_calls == [
        (
            repository.session.id,
            BookingIdentity("user-10", True),
            "partial_service_change_unsupported",
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Перенесите на третье августа",
        "Поменяйте мастера",
        "Можно выбрать Анну?",
        "Хочу время попозже",
        "Хочу изменить дату услуги на завтра",
        "Хочу поменять время услуги на вечер",
        "Можно поменять мастера для услуги?",
        "Дату услуги хочу изменить на завтра",
        "Мастера для услуги хочу поменять",
        "Время процедуры нужно поменять",
        "Времени для услуги хочу поменять",
        "Время хочу поменять для услуги",
        "Мастера хочу поменять для услуги",
        "Дату хочу изменить для услуги",
    ],
)
@pytest.mark.parametrize("kind", ["reschedule", "cancel"])
async def test_service_mention_does_not_override_date_time_or_staff_target(
    change_dependencies,
    text,
    kind,
):
    workflow, _repository, _catalog, _port, service = change_dependencies
    if kind == "reschedule":
        reply = await workflow.start_reschedule(OWNER, f"change:ordinary:{text}")
        await _press(workflow, OWNER, reply, _button_texts(reply)[0])
    else:
        reply = await workflow.start_cancel(OWNER, f"cancel:ordinary:{text}")
        await _press(workflow, OWNER, reply, _button_texts(reply)[0])

    await workflow.handle(Interaction.text(OWNER, "ordinary", text))

    assert service.escalate_calls == []
    assert service.handle_calls == []


@pytest.mark.asyncio
async def test_partial_service_request_during_cancel_workflow_escalates(
    change_dependencies,
):
    workflow, repository, _catalog, _port, service = change_dependencies
    await workflow.start_cancel(OWNER, "cancel:partial-service")

    result = await workflow.handle(
        Interaction.text(OWNER, "cancel:partial-service:text", "Хочу убрать услугу")
    )

    assert "администратор" in result.text.casefold()
    assert service.handle_calls == []
    assert service.escalate_calls == [
        (
            repository.session.id,
            BookingIdentity("user-10", True),
            "partial_service_change_unsupported",
        )
    ]


@pytest.mark.asyncio
async def test_foreign_or_replayed_booking_action_never_selects_record(
    change_dependencies,
):
    workflow, repository, _catalog, _port, service = change_dependencies
    reply = await workflow.start_cancel(OWNER, "cancel:opaque")
    callback = _callback(reply, _button_texts(reply)[0])

    foreign = await workflow.handle(
        Interaction.callback(
            BookingOwner("telegram", "user-11", "user-11"),
            "foreign:1",
            callback,
        )
    )
    assert "истёк" in foreign.text.casefold()
    assert repository.session.state.get("external_id") is None
    assert service.handle_calls == []


@pytest.mark.asyncio
async def test_multiple_bookings_use_only_opaque_index_actions(change_dependencies):
    workflow, repository, _catalog, port, _service = change_dependencies
    second = _booking(
        external_id="provider-secret-99",
        booking_key=UUID("00000000-0000-0000-0000-000000000999"),
        slot_id="slot-old-second",
        starts_at=OLD_START + timedelta(days=1),
        scheduled_end_at=OLD_START + timedelta(days=1, minutes=90),
    )
    repository.owned.append(second)
    port.providers[second.external_id] = second

    reply = await workflow.start_cancel(OWNER, "cancel:multiple")

    assert len(_button_texts(reply)) == 3
    rendered = json.dumps(reply.to_result(), ensure_ascii=False)
    assert second.external_id not in rendered
    assert str(second.booking_key) not in rendered
    selected = await _press(workflow, OWNER, reply, _button_texts(reply)[1])
    assert "03.08.2026 12:00" in selected.text


@pytest.mark.asyncio
async def test_reschedule_slot_race_returns_fresh_selection_and_preserves_old_start(
    change_dependencies,
):
    workflow, repository, _catalog, _port, service = change_dependencies
    reply = await workflow.start_reschedule(OWNER, "change:race")
    reply = await _press(workflow, OWNER, reply, _button_texts(reply)[0])
    reply = await _press(workflow, OWNER, reply, "Любой мастер")
    reply = await _press(workflow, OWNER, reply, _button_texts(reply)[0])
    summary = await _press(workflow, OWNER, reply, _button_texts(reply)[0])
    service.result = ScenarioResult(
        "needs_input",
        "provider detail",
        "choose_slot",
        (),
    )

    recovered = await _press(workflow, OWNER, summary, "Подтвердить")

    assert "недоступно" in recovered.text.casefold()
    assert repository.session.phase == "collecting"
    assert repository.session.state["step"] == "date"
    assert repository.session.state["starts_at"] == OLD_START.isoformat()
    assert "selected_new_starts_at" not in repository.session.state
