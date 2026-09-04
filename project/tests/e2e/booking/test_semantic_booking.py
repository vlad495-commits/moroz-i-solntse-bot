from dataclasses import replace
import json

import pytest

from tests.e2e.booking.test_telegram_booking import _coordinator, _handle, _button_labels, NOW
from tests.e2e.test_security_pipeline import _incoming, FakeTelegram
from moroz.messaging.router import RouteDecision, RouterVerdict
from moroz.security.pipeline import SecurityPipeline
from moroz.security.validator import extract_structured_facts
from moroz.security.llm_gateway import LLMResponse
from moroz.security.input_security import InputSecurityVerdict, InputSecurityDecision
from moroz.messaging.repository import MessageRepository
from moroz.messaging.telegram import TelegramSender
from moroz.messaging.outbox import process_message_key
from moroz.common.queue import QueueTask
from moroz.booking.yclients_catalog import CatalogRecord
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

    async def route(self, text, context):
        self.calls.append((text, context))
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
        assert row['text'] == 'Выберите время'
        assert '13:00' in str(row['delivery_options'])

        router.decision = RouteDecision('consultation', .99)
        await send('9002', 'Что такое водородотерапия?')
        assert (await bookings.get_active_for_customer('42')).id == draft.id
        assert len(router.calls) == 2
        assert 'slot' in str(router.calls[-1][1])

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
        reply = await _handle(coordinator, database, **base, update_id='a2', text='Спины',
            decision=RouteDecision('booking', .98, 'continue', 'Массаж спины'))
        assert reply.text == 'Выберите время'
        draft = await bookings.get_active_for_customer('42')
        assert draft.state['selected_date'] == '2026-09-05'
        assert adapter.create_calls == 0
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
        await _handle(coordinator, database, customer_id='42', user_id='7', kind='callback',
            data={'callback_data': old}, update_id='s3', text='')
        draft = await bookings.get_active_for_customer('42')
        assert draft.state['step'] == 'slot'
        assert draft.state['selected_date'] == '2026-09-06'
        assert adapter.create_calls == 0
    finally:
        await database.close()


async def test_slots_after_first_page_remain_selectable(migrated_database_url):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    try:
        scenario = BookingScenario(uuid4(), 'create', 'collecting', 'paging', '42',
            {'step': 'slot', 'choices': [dict(slot_id=f'slot-{i}', staff_id='10',
                starts_at=f'2026-09-07T{10+i:02}:00:00+03:00', label=f'{10+i}:00') for i in range(10)]},
            None, NOW, NOW)
        await bookings.create_scenario(scenario)
        first = coordinator._render_current(scenario)
        assert '19:00' not in _button_labels(first)
        next_page = first.delivery_options['reply_markup']['inline_keyboard'][-1][0]['callback_data']
        second = await _handle(coordinator, database, customer_id='42', user_id='7', kind='callback',
            data={'callback_data': next_page}, update_id='page', text='')
        assert '19:00' in _button_labels(second)
        assert adapter.create_calls == 0
    finally:
        await database.close()
