from dataclasses import replace
import json

import pytest

from tests.e2e.booking.test_telegram_booking import _coordinator, _handle, _button_labels, NOW
from tests.e2e.test_security_pipeline import _incoming, FakeTelegram
from moroz.messaging.router import LLMIntentRouter, RouteDecision, RouterVerdict
from moroz.security.pipeline import SecurityPipeline
from moroz.security.validator import extract_structured_facts
from moroz.security.llm_gateway import LLMResponse
from moroz.security.input_security import InputSecurityVerdict, InputSecurityDecision
from moroz.messaging.repository import MessageRepository
from moroz.messaging.telegram import TelegramSender
from moroz.messaging.outbox import process_message_key
from moroz.common.queue import QueueTask
from moroz.booking.yclients_catalog import CatalogRecord
from moroz.booking.catalog import CatalogRepository
from moroz.booking.models import BookingScenario
from uuid import uuid4
from worker.main import MessageTaskHandler

pytestmark = pytest.mark.asyncio


class Provider:
    async def complete(self, request):
        return LLMResponse('Это консультация об услуге.', 0, 0, 0, 0, 'test')


class Security:
    async def classify(self, text):
        return InputSecurityVerdict(InputSecurityDecision('allow', 'llm', 'ok'))


class Router:
    def __init__(self):
        self.calls = []
        self.decision = RouteDecision('booking', .99, 'create', 'Криокапсула', '2026-09-05')

    async def route(self, text, context, *, state=None):
        self.calls.append((text, context, state))
        return RouterVerdict(self.decision)


async def test_worker_dispatches_then_consults_and_preserves_draft(migrated_database_url):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    try:
        repository = MessageRepository(database)
        router = Router()
        pipeline = SecurityPipeline(Provider(), '', extract_structured_facts(''), router=router, input_security=Security())
        handler = MessageTaskHandler(database, pipeline.respond,
            TelegramSender(FakeTelegram(), repository), booking_coordinator=coordinator)

        async def send(update, text):
            await repository.accept(replace(_incoming(update), text=text))
            await handler.handle(QueueTask(kind='process_message',
                payload={'chat_id': '42', 'update_ids': [update]},
                idempotency_key=process_message_key([update])))

        await send('9001', 'Покажи свободное время на криокапсулу 5 сентября')
        draft = await bookings.get_active_for_customer('42')
        assert draft.state['step'] == 'slot'
        assert draft.state['selected_date'] == '2026-09-05'
        assert len(router.calls) == 1
        assert adapter.create_calls == 0
        async with database.acquire() as connection:
            row = await connection.fetchrow('SELECT text, delivery_options FROM outbound_messages ORDER BY created_at DESC LIMIT 1')
        assert row['text'] == 'Выберите время\nКриокапсула\n05.09.2026 · московское время'
        assert '13:00' in str(row['delivery_options'])

        router.decision = RouteDecision('consultation', .99)
        await send('9002', 'Что такое водородотерапия?')
        assert (await bookings.get_active_for_customer('42')).id == draft.id
        assert len(router.calls) == 2
        assert 'slot' in str(router.calls[-1][2])

        router.decision = RouteDecision('booking_management', .99, 'view')
        await send('9003', 'Куда я записывался?')
        assert await bookings.get_active_for_customer('42') is None
        assert adapter.create_calls == 0
    finally:
        await database.close()


async def test_plain_question_does_not_get_swallowed_without_decision(migrated_database_url):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    base = dict(customer_id='42', user_id='7', kind='text', data={})
    try:
        await _handle(coordinator, database, **base, update_id='start', text='📅 Записаться')
        draft = await bookings.get_active_for_customer('42')
        result = await _handle(coordinator, database, **base, update_id='question', text='Что такое водородотерапия?')
        assert result is None
        assert (await bookings.get_active_for_customer('42')).id == draft.id
        assert adapter.create_calls == 0
    finally:
        await database.close()


async def test_ambiguous_service_keeps_date_until_service_selection(migrated_database_url):
    records = tuple(CatalogRecord(str(i), '10', name, 'Массаж', 'Анна', 1000, 1000, 60)
                    for i, name in [(331, 'Массаж спины'), (332, 'Общий массаж тела'), (333, 'LED маска')])
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url, catalog_records=records)
    base = dict(customer_id='42', user_id='7', kind='text', data={})
    try:
        reply = await _handle(coordinator, database, **base, update_id='a1', text='Массаж 5 сентября',
            decision=RouteDecision('booking', .98, 'create', 'массаж', '2026-09-05'))
        assert set(_button_labels(reply)) == {'Массаж спины', 'Общий массаж тела'}
        reply = await _handle(coordinator, database, **base, update_id='repeat', text='На какие часы свободно?',
            decision=RouteDecision('booking', .98, 'continue'))
        assert '05.09.2026' in reply.text
        assert 'зависит от' in reply.text
        reply = await _handle(coordinator, database, **base, update_id='a2', text='Спины',
            decision=RouteDecision('booking', .98, 'continue', 'Массаж спины'))
        assert reply.text == 'Выберите время\nМассаж спины\n05.09.2026 · московское время'
        draft = await bookings.get_active_for_customer('42')
        assert draft.state['selected_date'] == '2026-09-05'
        assert adapter.create_calls == 0
    finally:
        await database.close()


async def test_catalog_menu_shows_prices_then_opens_booking(migrated_database_url):
    records = (CatalogRecord('331', '10', 'Массаж спины', 'Массаж', 'Анна', 1500, 1500, 30),)
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url, catalog_records=records)
    base = dict(customer_id='42', user_id='7')
    try:
        reply = await _handle(coordinator, database, **base, kind='text', data={}, update_id='prices', text='✨ Услуги и цены')
        assert reply is not None
        assert 'Массаж' in _button_labels(reply)

        async def click(reply, update):
            token = reply.delivery_options['reply_markup']['inline_keyboard'][0][0]['callback_data']
            assert len(token.encode()) <= 64
            return await _handle(coordinator, database, **base, kind='callback', data={'callback_data': token}, update_id=update, text='')

        reply = await click(reply, 'category')
        assert '1 500 ₽' in reply.text
        assert '30 мин.' in reply.text
        reply = await click(reply, 'detail')
        assert '1 500 ₽' in reply.text
        state = json.loads(await coordinator.routing_context('42'))
        assert state['mode'] == 'catalog_browse'
        assert state['service'] == 'Массаж спины'
        assert 'Свободное время' in _button_labels(reply)
        reply = await click(reply, 'start-booking')
        assert (await bookings.get_active_for_customer('42')).state['step'] == 'staff'
        assert adapter.create_calls == 0
    finally:
        await database.close()


async def test_catalog_walk_in_family_expands_exact_durations_in_numeric_order(
    migrated_database_url,
):
    records = tuple(
        CatalogRecord(
            str(minutes), "10", f"Солярий {minutes} минут", "Загар",
            "Солярий", minutes * 100, minutes * 100, minutes,
        )
        for minutes in (1, 7, 10)
    )
    database, bookings, adapter, coordinator = await _coordinator(
        migrated_database_url, catalog_records=records,
    )
    base = dict(customer_id="42", user_id="7")
    try:
        reply = await _handle(
            coordinator, database, **base, kind="text", data={},
            update_id="family-menu", text="✨ Услуги и цены",
        )
        reply = await _handle(
            coordinator, database, **base, kind="callback",
            data={"callback_data": reply.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]},
            update_id="family-category", text="",
        )

        assert _button_labels(reply) == [
            "Солярий 1 минут", "Солярий 7 минут", "Солярий 10 минут",
        ]
        assert reply.text.index("100 ₽") < reply.text.index("700 ₽") < reply.text.index("1 000 ₽")
        seven = reply.delivery_options["reply_markup"]["inline_keyboard"][1][0]["callback_data"]
        detail = await _handle(
            coordinator, database, **base, kind="callback",
            data={"callback_data": seven}, update_id="family-seven", text="",
        )
        assert "«Солярий 7 минут» — 700 ₽, 7 мин." in detail.text
        assert (adapter.list_calls, adapter.create_calls, adapter.reschedule_calls,
                adapter.cancel_calls) == (0, 0, 0, 0)
        assert (await bookings.get_active_for_customer("42")).state["catalog_service_id"] == "7"
    finally:
        await database.close()


async def test_catalog_walk_in_family_keeps_selected_page(migrated_database_url):
    records = tuple(
        CatalogRecord(
            str(minutes), "10", f"Солярий {minutes} минут", "Загар",
            "Солярий", minutes * 100, minutes * 100, minutes,
        )
        for minutes in range(1, 11)
    )
    database, _, adapter, coordinator = await _coordinator(
        migrated_database_url, catalog_records=records,
    )
    base = dict(customer_id="42", user_id="7")
    try:
        reply = await _handle(
            coordinator, database, **base, kind="text", data={},
            update_id="family-page-menu", text="✨ Услуги и цены",
        )
        first = await _handle(
            coordinator, database, **base, kind="callback",
            data={"callback_data": reply.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]},
            update_id="family-page-category", text="",
        )
        next_page = first.delivery_options["reply_markup"]["inline_keyboard"][-1][0]["callback_data"]
        second = await _handle(
            coordinator, database, **base, kind="callback",
            data={"callback_data": next_page}, update_id="family-page-next", text="",
        )

        labels = _button_labels(second)
        assert "Солярий 9 минут" in labels
        assert "Солярий 10 минут" in labels
        assert "Солярий 1 минут" not in labels
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (0, 0, 0)
    finally:
        await database.close()


async def test_mixed_catalog_groups_walk_in_family_before_regular_service(
    migrated_database_url,
):
    records = (
        CatalogRecord("10", "10", "Солярий 10 минут", "Загар", "Солярий", 1000, 1000, 10),
        CatalogRecord("1", "10", "Солярий 1 минута", "Загар", "Солярий", 100, 100, 1),
        CatalogRecord("50", "10", "Депозит на загар", "Загар", "Касса", 1500, 1500, 60),
    )
    database, _, adapter, coordinator = await _coordinator(
        migrated_database_url, catalog_records=records,
    )
    base = dict(customer_id="42", user_id="7")
    try:
        reply = await _handle(
            coordinator, database, **base, kind="text", data={},
            update_id="mixed-menu", text="✨ Услуги и цены",
        )
        reply = await _handle(
            coordinator, database, **base, kind="callback",
            data={"callback_data": reply.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]},
            update_id="mixed-category", text="",
        )
        assert _button_labels(reply) == ["Солярий", "Депозит на загар"]
        assert "Депозит на загар — 1 500 ₽" in reply.text
        assert "60 мин." not in reply.text

        family = reply.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        variants = await _handle(
            coordinator, database, **base, kind="callback",
            data={"callback_data": family}, update_id="mixed-family", text="",
        )
        assert _button_labels(variants) == ["Солярий 1 минута", "Солярий 10 минут"]
        assert (adapter.list_calls, adapter.create_calls, adapter.reschedule_calls,
                adapter.cancel_calls) == (0, 0, 0, 0)
    finally:
        await database.close()


async def test_closed_catalog_callback_returns_to_price_navigation(
    migrated_database_url,
):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    base = dict(customer_id="42", user_id="7")
    try:
        reply = await _handle(
            coordinator, database, **base, kind="text", data={},
            update_id="closed-menu", text="✨ Услуги и цены",
        )
        old = reply.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        scenario = await bookings.get_active_for_customer("42")
        await bookings.checkpoint(replace(scenario, phase="failed"), "catalog_closed")

        stale = await _handle(
            coordinator, database, **base, kind="callback",
            data={"callback_data": old}, update_id="closed-callback", text="",
        )
        assert "Начните запись" not in stale.text
        assert "Услуги и цены" in stale.text
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (0, 0, 0)
    finally:
        await database.close()


async def test_old_catalog_root_callback_after_menu_exit_returns_to_prices(
    migrated_database_url,
):
    database, _, adapter, coordinator = await _coordinator(migrated_database_url)
    base = dict(customer_id="42", user_id="7")
    try:
        root = await _handle(
            coordinator, database, **base, kind="text", data={},
            update_id="old-root-menu", text="✨ Услуги и цены",
        )
        old = root.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        await _handle(
            coordinator, database, **base, kind="callback",
            data={"callback_data": old}, update_id="old-root-open", text="",
        )
        await _handle(
            coordinator, database, **base, kind="text", data={},
            update_id="old-root-exit", text="📍 Адрес и режим",
        )

        stale = await _handle(
            coordinator, database, **base, kind="callback",
            data={"callback_data": old}, update_id="old-root-click", text="",
        )
        assert "Начните запись" not in stale.text
        assert "Услуги и цены" in stale.text
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (0, 0, 0)
    finally:
        await database.close()


async def test_old_catalog_page_callback_after_menu_exit_returns_to_prices(
    migrated_database_url,
):
    records = tuple(
        CatalogRecord(str(index), "10", f"Массаж {index}", "Массаж", "Анна", 1500, 1500, 30)
        for index in range(9)
    )
    database, _, adapter, coordinator = await _coordinator(
        migrated_database_url, catalog_records=records,
    )
    base = dict(customer_id="42", user_id="7")
    try:
        root = await _handle(
            coordinator, database, **base, kind="text", data={},
            update_id="old-page-menu", text="✨ Услуги и цены",
        )
        listing = await _handle(
            coordinator, database, **base, kind="callback",
            data={"callback_data": root.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]},
            update_id="old-page-open", text="",
        )
        old_page = listing.delivery_options["reply_markup"]["inline_keyboard"][-1][0]["callback_data"]
        await _handle(
            coordinator, database, **base, kind="text", data={},
            update_id="old-page-exit", text="📍 Адрес и режим",
        )

        stale = await _handle(
            coordinator, database, **base, kind="callback",
            data={"callback_data": old_page}, update_id="old-page-click", text="",
        )
        assert "Начните запись" not in stale.text
        assert "Услуги и цены" in stale.text
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (0, 0, 0)
    finally:
        await database.close()


async def test_removed_category_reopens_current_catalog_root(migrated_database_url):
    records = (
        CatalogRecord("331", "10", "Массаж спины", "Массаж", "Анна", 1500, 1500, 30),
        CatalogRecord("332", "10", "Криокапсула", "Крио", "Анна", 1000, 1000, 60),
    )
    database, _, adapter, coordinator = await _coordinator(
        migrated_database_url, catalog_records=records,
    )
    base = dict(customer_id="42", user_id="7")
    try:
        root = await _handle(
            coordinator, database, **base, kind="text", data={},
            update_id="removed-category-menu", text="✨ Услуги и цены",
        )
        massage = next(
            button["callback_data"]
            for row in root.delivery_options["reply_markup"]["inline_keyboard"]
            for button in row
            if button["text"] == "Массаж"
        )
        async with database.acquire() as connection:
            await connection.execute(
                "DELETE FROM yclients_service_catalog WHERE category_name = 'Массаж'"
            )

        stale = await _handle(
            coordinator, database, **base, kind="callback",
            data={"callback_data": massage}, update_id="removed-category-click", text="",
        )
        assert "неактуальна" in stale.text.casefold()
        assert _button_labels(stale) == ["Крио"]
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (0, 0, 0)
    finally:
        await database.close()


async def test_removed_open_family_reopens_current_catalog_root(migrated_database_url):
    records = (
        *tuple(
            CatalogRecord(str(minutes), "10", f"Солярий {minutes} минут", "Загар",
                          "Солярий", minutes * 100, minutes * 100, minutes)
            for minutes in range(1, 10)
        ),
        CatalogRecord("332", "10", "Криокапсула", "Крио", "Анна", 1000, 1000, 60),
    )
    database, _, adapter, coordinator = await _coordinator(
        migrated_database_url, catalog_records=records,
    )
    base = dict(customer_id="42", user_id="7")
    try:
        root = await _handle(
            coordinator, database, **base, kind="text", data={},
            update_id="removed-family-menu", text="✨ Услуги и цены",
        )
        tanning = next(
            button["callback_data"]
            for row in root.delivery_options["reply_markup"]["inline_keyboard"]
            for button in row
            if button["text"] == "Загар"
        )
        listing = await _handle(
            coordinator, database, **base, kind="callback",
            data={"callback_data": tanning}, update_id="removed-family-open", text="",
        )
        next_page = listing.delivery_options["reply_markup"]["inline_keyboard"][-1][0]["callback_data"]
        async with database.acquire() as connection:
            await connection.execute(
                "DELETE FROM yclients_service_catalog WHERE category_name = 'Загар'"
            )

        stale = await _handle(
            coordinator, database, **base, kind="callback",
            data={"callback_data": next_page}, update_id="removed-family-click", text="",
        )
        assert "неактуальна" in stale.text.casefold()
        assert _button_labels(stale) == ["Крио"]
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (0, 0, 0)
    finally:
        await database.close()


async def test_old_slot_button_cannot_select_new_date(migrated_database_url):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    base = dict(customer_id='42', user_id='7', kind='text', data={})
    try:
        first = await _handle(coordinator, database, **base, update_id='s1', text='5 сентября',
            decision=RouteDecision('booking', .99, 'create', 'Криокапсула', '2026-09-05'))
        old = first.delivery_options['reply_markup']['inline_keyboard'][0][0]['callback_data']
        await _handle(coordinator, database, **base, update_id='s2', text='Лучше 6 сентября',
            decision=RouteDecision('booking', .99, 'continue', date='2026-09-06'))
        stale = await _handle(coordinator, database, customer_id='42', user_id='7', kind='callback',
            data={'callback_data': old}, update_id='s3', text='')
        draft = await bookings.get_active_for_customer('42')
        assert draft.state['step'] == 'slot'
        assert draft.state['selected_date'] == '2026-09-06'
        assert 'кнопка уже неактуальна' in stale.text.casefold()
        assert 'Криокапсула\n06.09.2026 · московское время' in stale.text
        assert adapter.create_calls == 0
    finally:
        await database.close()


async def test_slots_after_first_page_remain_selectable(migrated_database_url):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    try:
        scenario = BookingScenario(uuid4(), 'create', 'collecting', 'paging', '42',
            {'step': 'slot', 'service_name': 'Массаж спины', 'selected_date': '2026-09-07',
             'choices': [dict(slot_id=f'slot-{i}', staff_id='10',
                starts_at=f'2026-09-07T{10+i:02}:00:00+03:00', label=f'{10+i}:00') for i in range(10)]},
            None, NOW, NOW)
        await bookings.create_scenario(scenario)
        first = coordinator._render_current(scenario)
        assert '19:00' not in _button_labels(first)
        next_page = first.delivery_options['reply_markup']['inline_keyboard'][-1][0]['callback_data']
        second = await _handle(coordinator, database, customer_id='42', user_id='7', kind='callback',
            data={'callback_data': next_page}, update_id='page', text='')
        assert second.text == 'Выберите время\nМассаж спины\n07.09.2026 · московское время'
        assert '19:00' in _button_labels(second)
        assert adapter.create_calls == 0
    finally:
        await database.close()


async def test_worker_prices_use_semantic_service_and_do_not_guess_ambiguous(migrated_database_url):
    records = tuple(CatalogRecord(str(i), '10', name, 'Крио', 'Анна', price, price, 30)
        for i, name, price in [(331, 'Криомассаж головы', 1500), (332, 'Криомассаж лица', 2000)])
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url, catalog_records=records)
    try:
        repository = MessageRepository(database)
        router = Router()
        pipeline = SecurityPipeline(Provider(), '', extract_structured_facts(''), router=router, input_security=Security())
        handler = MessageTaskHandler(database, pipeline.respond, TelegramSender(FakeTelegram(), repository),
            catalog_repository=CatalogRepository(database), catalog_grounding_enabled=True, clock=lambda: NOW)
        async def send(update, text, service):
            router.decision = RouteDecision('consultation', .99, 'price', service=service)
            await repository.accept(replace(_incoming(update), text=text))
            await handler.handle(QueueTask(kind='process_message', payload={'chat_id': '42', 'update_ids': [update]},
                idempotency_key=process_message_key([update])))
            async with database.acquire() as connection:
                return await connection.fetchval('SELECT text FROM outbound_messages ORDER BY created_at DESC LIMIT 1')
        answer = await send('price1', 'Сколько стоит?', 'Криомассаж головы')
        assert '1 500 ₽' in answer
        assert '2 000' not in answer
        answer = await send('price2', 'А криомассаж сколько?', 'Криомассаж')
        assert 'уточните' in answer.casefold()
        assert '1 500' not in answer
        answer = await send('price3', 'А другое сколько?', None)
        assert 'уточните' in answer.casefold()
        assert '1 500' not in answer
    finally:
        await database.close()


async def test_catalog_menu_retry_keeps_live_buttons(migrated_database_url):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    base = dict(customer_id='42', user_id='7', kind='text', data={}, update_id='retry-menu', text='✨ Услуги и цены')
    try:
        await _handle(coordinator, database, **base)
        reply = await _handle(coordinator, database, **base)
        active = await bookings.get_active_for_customer('42')
        assert active is not None
        token = reply.delivery_options['reply_markup']['inline_keyboard'][0][0]['callback_data']
        reply = await _handle(coordinator, database, customer_id='42', user_id='7', kind='callback',
            data={'callback_data': token}, update_id='retry-click', text='')
        assert '₽' in reply.text
    finally:
        await database.close()


@pytest.mark.parametrize('semantic', [False, True])
async def test_create_retry_keeps_same_usable_draft(migrated_database_url, semantic):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    request = dict(customer_id='42', user_id='7', kind='text', data={},
        update_id='retry-create', origin_update_id='batch-last',
        text='Запиши на криокапсулу' if semantic else '📅 Записаться',
        decision=RouteDecision('booking', .99, 'create', 'Криокапсула') if semantic else None)
    try:
        first = await _handle(coordinator, database, **request)
        draft = await bookings.get_active_for_customer('42')
        reply = await _handle(coordinator, database, **request)
        active = await bookings.get_active_for_customer('42')
        assert active is not None
        assert active.id == draft.id
        assert active.phase == 'collecting'
        assert active.state == draft.state
        assert active.state['origin_update_id'] == 'batch-last'
        assert active.state['source'] == 'telegram'
        assert reply == first
        token = reply.delivery_options['reply_markup']['inline_keyboard'][0][0]['callback_data']
        reply = await _handle(coordinator, database, customer_id='42', user_id='7', kind='callback',
            data={'callback_data': token}, update_id='retry-create-click', text='')
        active = await bookings.get_active_for_customer('42')
        assert active.id == draft.id
        assert active.state['step'] == ('available_date' if semantic else 'staff')
        assert 'неактуальна' not in reply.text
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (0, 0, 0)
    finally:
        await database.close()


@pytest.mark.parametrize('semantic', [False, True])
async def test_worker_create_retry_after_outbound_failure_keeps_single_draft(
    migrated_database_url, monkeypatch, semantic,
):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    try:
        repository = MessageRepository(database)
        pipeline = SecurityPipeline(Provider(), '', extract_structured_facts(''),
            router=Router(), input_security=Security())
        handler = MessageTaskHandler(database, pipeline.respond,
            TelegramSender(FakeTelegram(), repository), booking_coordinator=coordinator)
        await repository.accept(replace(_incoming('9101'),
            text='Запиши на криокапсулу 5 сентября' if semantic else '📅 Записаться'))
        task = QueueTask(kind='process_message', payload={'chat_id': '42', 'update_ids': ['9101']},
            idempotency_key=process_message_key(['9101']))

        async def crash_before_commit(*args, **kwargs):
            raise RuntimeError('injected outbound failure before inbox commit')

        with monkeypatch.context() as patch:
            patch.setattr(handler._repository, 'enqueue_outbound_in_transaction', crash_before_commit)
            with pytest.raises(RuntimeError, match='injected outbound failure'):
                await handler.handle(task)
        draft = await bookings.get_active_for_customer('42')
        assert draft is not None
        async with database.acquire() as connection:
            assert await connection.fetchval('SELECT count(*) FROM messages') == 0
            assert await connection.fetchval('SELECT count(*) FROM outbound_messages') == 0
            assert await connection.fetchval('SELECT status FROM message_inbox') != 'processed'

        await handler.handle(task)
        await handler.handle(task)
        active = await bookings.get_active_for_customer('42')
        assert active is not None
        assert active.id == draft.id
        assert active.state == draft.state
        async with database.acquire() as connection:
            assert await connection.fetchval('SELECT count(*) FROM booking_scenarios') == 1
            assert await connection.fetchval('SELECT count(*) FROM messages') == 2
            assert await connection.fetchval('SELECT count(*) FROM outbound_messages') == 1
            assert await connection.fetchval('SELECT status FROM message_inbox') == 'processed'
            reply = await connection.fetchrow('SELECT text, delivery_options FROM outbound_messages')
        assert reply['text'].startswith('Выберите время' if semantic else 'Выберите услугу')
        assert 'inline_keyboard' in str(reply['delivery_options'])
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (0, 0, 0)
    finally:
        await database.close()


@pytest.mark.parametrize('phase', ['failed', 'confirmed'])
async def test_create_retry_does_not_reopen_terminal_draft(migrated_database_url, phase):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    request = dict(customer_id='42', user_id='7', kind='text', data={},
        update_id='terminal-create', text='📅 Записаться')
    try:
        await _handle(coordinator, database, **request)
        draft = await bookings.get_active_for_customer('42')
        await bookings.checkpoint(replace(draft, phase=phase), 'test_terminal')
        await _handle(coordinator, database, **request)
        assert await bookings.get_active_for_customer('42') is None
        assert (await bookings.get_scenario(draft.id)).phase == phase
        assert adapter.create_calls == 0
    finally:
        await database.close()


async def test_catalog_paging_and_recovery_do_not_reuse_stale_prices(migrated_database_url):
    records = tuple(CatalogRecord(str(i + 331), '10', f'Массаж {i}', 'Массаж', 'Анна', 1500, 1500, 30) for i in range(10))
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url, catalog_records=records)
    try:
        reply = await _handle(coordinator, database, customer_id='42', user_id='7', kind='text', data={}, update_id='stale-menu', text='✨ Услуги и цены')
        token = reply.delivery_options['reply_markup']['inline_keyboard'][0][0]['callback_data']
        reply = await _handle(coordinator, database, customer_id='42', user_id='7', kind='callback', data={'callback_data': token}, update_id='stale-category', text='')
        next_page = reply.delivery_options['reply_markup']['inline_keyboard'][-1][0]['callback_data']
        async with database.acquire() as connection:
            await connection.execute("DELETE FROM yclients_service_catalog WHERE service_id::int > 332")
        reply = await _handle(coordinator, database, customer_id='42', user_id='7', kind='callback', data={'callback_data': next_page}, update_id='shrunk-page', text='')
        assert '1 500' in reply.text
        async with database.acquire() as connection:
            await connection.execute("UPDATE yclients_service_catalog SET synced_at = synced_at - interval '2 days'")
        for update, callback in [('stale-page', next_page), ('stale-recover', token)]:
            reply = await _handle(coordinator, database, customer_id='42', user_id='7', kind='callback', data={'callback_data': callback}, update_id=update, text='')
            assert '1 500' not in reply.text
            assert 'актуальн' in reply.text
    finally:
        await database.close()


@pytest.mark.parametrize('decision', [
    RouteDecision('booking', .99, 'continue', 'Криокапсула'),
    RouteDecision('booking', .99),
    RouteDecision('booking', .99, 'provide_name'),
    RouteDecision('booking', .99, 'create', choice=0),
    RouteDecision('booking_management', .99, 'create'),
])
async def test_only_explicit_valid_create_can_open_new_booking(migrated_database_url, decision):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    try:
        await _handle(coordinator, database, customer_id='42', user_id='7', kind='text', data={},
            update_id='unsafe-start', text='Криокапсула', decision=decision)
        assert await bookings.get_active_for_customer('42') is None
        assert adapter.create_calls == 0
    finally:
        await database.close()


async def test_browse_continue_preserves_catalog_but_create_starts_booking(migrated_database_url):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    base = dict(customer_id='42', user_id='7', kind='text', data={})
    try:
        await _handle(coordinator, database, **base, update_id='browse', text='✨ Услуги и цены')
        draft = await bookings.get_active_for_customer('42')
        await _handle(coordinator, database, **base, update_id='bare', text='Криокапсула',
            decision=RouteDecision('booking', .99, 'continue', 'Криокапсула'))
        active = await bookings.get_active_for_customer('42')
        assert active.id == draft.id
        assert active.state['step'] == 'catalog_category'
        await _handle(coordinator, database, **base, update_id='explicit', text='Запиши на криокапсулу',
            decision=RouteDecision('booking', .99, 'create', 'Криокапсула'))
        assert (await bookings.get_active_for_customer('42')).state['step'] == 'staff'
        assert adapter.create_calls == 0
    finally:
        await database.close()


async def test_unknown_clarification_is_not_cancellation(migrated_database_url):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    base = dict(customer_id='42', user_id='7', kind='text', data={})
    try:
        for route in ('booking', 'other'):
            reply = await _handle(coordinator, database, **base, update_id=route, text='ghbdtn',
                decision=RouteDecision(route, .99, 'clarify'))
            assert reply is not None
            assert 'отмен' not in reply.text.casefold()
        reply = await _handle(coordinator, database, **base, update_id='cancel', text='Отмени',
            decision=RouteDecision('booking', .99, 'clarify_cancel'))
        assert 'отменить' in reply.text
        assert await bookings.get_active_for_customer('42') is None
    finally:
        await database.close()


async def test_catalog_state_preserves_conversation_and_pii_boundary(migrated_database_url):
    records = tuple(CatalogRecord(str(i + 331), '10', f'Массаж {i} ' + 'расширенное название ' * 15,
        'Массаж', 'Анна', 1500, 1500, 30) for i in range(76))
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url, catalog_records=records)
    base = dict(customer_id='42', user_id='7')
    try:
        reply = await _handle(coordinator, database, **base, kind='text', data={}, update_id='ctx-menu', text='✨ Услуги и цены')
        token = reply.delivery_options['reply_markup']['inline_keyboard'][0][0]['callback_data']
        await _handle(coordinator, database, **base, kind='callback', data={'callback_data': token}, update_id='ctx-cat', text='')
        draft = await bookings.get_active_for_customer('42')
        await bookings.checkpoint(replace(draft, state={**coordinator._state(draft), 'page': 1}), 'test_page')
        state = json.loads(await coordinator.routing_context('42'))
        assert state['mode'] == 'catalog_browse'
        assert state['active'] is False
        assert 'kind' not in state
        assert [item['index'] for item in state['choices']] == list(range(8, 16))
        state['service'] = 'Массаж +7 900 111-22-33'

        class CapturingProvider:
            def __init__(self):
                self.requests = []
            async def complete(self, request):
                self.requests.append(request)
                text = ('{"route":"consultation","confidence":0.99,"action":"none"}'
                        if request.purpose == 'router' else 'Уточните вид массажа.')
                return LLMResponse(text, 0, 0, 0, 0, 'test')

        provider = CapturingProvider()
        pipeline = SecurityPipeline(provider, '', extract_structured_facts(''),
            router=LLMIntentRouter(provider), input_security=Security())
        await pipeline.respond('Массаж', [
            {'role': 'user', 'content': 'Сколько стоит массаж? Мой телефон +7 900 111-22-33'},
            {'role': 'assistant', 'content': 'Какой вид массажа вас интересует?'}],
            booking_context=json.dumps(state, ensure_ascii=False))
        routing, answer = provider.requests
        assert 'Какой вид массажа' in routing.messages[1]['content']
        assert 'Сколько стоит массаж?' in routing.messages[1]['content']
        actual = json.loads(routing.messages[2]['content'].split('\n', 1)[1])
        assert actual['choices'][0]['index'] == 8
        assert actual['mode'] == 'catalog_browse'
        assert '<PII_PHONE_1>' in actual['service']
        assert '+7 900' not in str(provider.requests)
        assert 'catalog_browse' not in str(answer.messages)
        assert 'UNTRUSTED_STATE' not in str(answer.messages)
        assert adapter.create_calls == 0
    finally:
        await database.close()


@pytest.mark.parametrize('action', ['continue', 'create'])
async def test_booking_continue_can_choose_active_management_action(migrated_database_url, action):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    try:
        scenario = BookingScenario(uuid4(), 'create', 'collecting', 'manage-choice', '42', {
            'step': 'booking_action', 'choices': [{'operation': 'cancel', 'label': 'Отменить'}],
            'selected_booking': {'external_id': 'owned-record', 'booking_key': str(uuid4()),
                'starts_at': '2026-09-05T13:00:00+03:00', 'service_id': '331',
                'service_name': 'Криокапсула', 'staff_name': 'Анна', 'staff_names': {}}},
            None, NOW, NOW)
        await bookings.create_scenario(scenario)
        assert json.loads(await coordinator.routing_context('42'))['mode'] == 'booking_management'
        reply = await _handle(coordinator, database, customer_id='42', user_id='7', kind='text', data={},
            update_id='manage-continue', text='Первый вариант' if action == 'continue' else 'Запиши на криокапсулу',
            decision=RouteDecision('booking', .99, action, 'Криокапсула' if action == 'create' else None,
                                   choice=0 if action == 'continue' else None))
        assert 'reply_markup' in reply.delivery_options
        if action == 'continue':
            assert 'Да, отменить' in _button_labels(reply)
        assert (await bookings.get_active_for_customer('42')).state['step'] == ('staff' if action == 'create' else 'confirm_change')
        assert adapter.cancel_calls == 0
    finally:
        await database.close()


@pytest.mark.parametrize('switch_service', [True, False])
async def test_explicit_switch_or_repeated_service_preserves_requested_date(migrated_database_url, switch_service):
    records = (CatalogRecord('331', '10', 'Массаж спины', 'Массаж', 'Анна', 1500, 1500, 60),
               CatalogRecord('332', '10', 'Прессотерапия', 'Тело', 'Анна', 1500, 1500, 60))
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url, catalog_records=records)
    base = dict(customer_id='42', user_id='7', kind='text', data={})
    try:
        await _handle(coordinator, database, **base, update_id='switch-start', text='Запиши на массаж',
            decision=RouteDecision('booking', .99, 'create', 'Массаж спины', '2026-09-05'))
        original = await bookings.get_active_for_customer('42')
        assert original.state['step'] == 'slot'
        service = 'Прессотерапия' if switch_service else 'Массаж спины'
        await _handle(coordinator, database, **base, update_id='switch-next', text='Лучше 6 сентября',
            decision=RouteDecision('booking', .99, 'create' if switch_service else 'continue', service, '2026-09-06'))
        draft = await bookings.get_active_for_customer('42')
        assert draft.state['requested_date'] == '2026-09-06'
        assert draft.state['service_name'] == service
        if not switch_service:
            assert draft.id == original.id
        state = json.loads(await coordinator.routing_context('42'))
        assert state['requested_date'] == '2026-09-06'
        assert state['service'] == service
        assert adapter.create_calls == 0
    finally:
        await database.close()


@pytest.mark.parametrize('catalog_change', ['stale', 'removed', 'updated'])
async def test_service_continuation_revalidates_current_catalog(migrated_database_url, catalog_change):
    records = (CatalogRecord('331', '10', 'Массаж спины', 'Массаж', 'Анна', 1500, 1500, 60),
               CatalogRecord('332', '10', 'Массаж лица', 'Массаж', 'Анна', 1500, 1500, 60))
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url, catalog_records=records)
    base = dict(customer_id='42', user_id='7', kind='text', data={})
    try:
        await _handle(coordinator, database, **base, update_id='fresh-start', text='Массаж завтра',
            decision=RouteDecision('booking', .99, 'create', 'массаж', '2026-09-05'))
        original = await bookings.get_active_for_customer('42')
        assert original.state['step'] == 'service'
        async with database.acquire() as connection:
            if catalog_change == 'stale':
                await connection.execute("UPDATE yclients_service_catalog SET synced_at = synced_at - interval '2 days'")
            elif catalog_change == 'removed':
                await connection.execute("DELETE FROM yclients_service_catalog WHERE service_id = '331'")
            else:
                await connection.execute("UPDATE yclients_service_catalog SET service_name = 'Массаж спины обновлённый', staff_id = '22', staff_name = 'Ольга' WHERE service_id = '331'")
        reply = await _handle(coordinator, database, **base, update_id='fresh-choose', text='Массаж спины',
            decision=RouteDecision('booking', .99, 'continue', 'Массаж спины'))
        draft = await bookings.get_active_for_customer('42')
        assert draft.id == original.id
        assert draft.state['requested_date'] == '2026-09-05'
        if catalog_change == 'updated':
            assert draft.state['service_name'] == 'Массаж спины обновлённый'
            assert dict(draft.state['staff_names']) == {'22': 'Ольга'}
        else:
            assert draft.state['step'] == 'service'
            assert adapter.list_calls == 0
            assert 'актуальн' in reply.text or 'больше нет' in reply.text
        assert adapter.create_calls == 0
    finally:
        await database.close()
