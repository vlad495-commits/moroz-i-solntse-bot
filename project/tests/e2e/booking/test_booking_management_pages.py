import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from moroz.booking.models import BookingScenario, Slot
from moroz.booking.service import BookingService
from moroz.booking.telegram import STALE_REPLY
from moroz.messaging.router import MAX_CHOICE_INDEX, RouteDecision
from tests.e2e.booking.telegram_helpers import NOW, coordinator, handle


pytestmark = pytest.mark.asyncio


def buttons(reply):
    return [button for row in reply.delivery_options['reply_markup']['inline_keyboard']
            for button in row]


async def seeded(url):
    slots = [Slot(f'slot-{i}', ('331',), '10', NOW + timedelta(days=i + 1), 60)
             for i in range(6)]
    database, repository, adapter, flow = await coordinator(url, slots=slots)
    service = BookingService(adapter, repository, now=lambda: NOW)
    for i, slot in enumerate(slots[:5]):
        scenario = BookingScenario(uuid4(), 'create', 'awaiting_confirmation',
            f'seed:{i}', '42' if i < 4 else 'other', {
                'service_id': '331', 'service_name': 'Криокапсула',
                'staff_name': 'Анна', 'staff_names': {'10': 'Анна'},
                'selected_staff_id': '10', 'selected_slot_id': slot.id,
                'customer_name': 'Иван', 'customer_phone': '+79990000000',
                'personal_data_processing_allowed': True,
                'slot_query': {'service_ids': ['331'], 'starts_after': NOW.isoformat(),
                               'starts_before': (NOW + timedelta(days=10)).isoformat()},
            }, None, NOW, NOW)
        await repository.create_scenario(scenario)
        await service.handle(scenario.id, confirmed=True)
    assert adapter.create_calls == 5
    return database, repository, adapter, flow


async def send(flow, database, *, callback=None, customer='42', update='message', decision=None):
    return await handle(flow, database, customer_id=customer, user_id='7',
        update_id=update, text='', kind='callback' if callback else 'text',
        data={'callback_data': callback} if callback else {}, decision=decision)


@pytest.mark.parametrize('operation', ['cancel', 'reschedule'])
@pytest.mark.parametrize('selection', ['button', 'text'])
async def test_fourth_booking_selected_and_changed_once(migrated_database_url, operation, selection):
    database, repository, adapter, flow = await seeded(migrated_database_url)
    try:
        owned = await repository.list_future_owned('42', NOW)
        original = [booking for booking, _ in owned]
        first = await send(flow, database, decision=RouteDecision(
            'booking_management', 1, operation, date='2026-09-10'))
        first_buttons = buttons(first)
        assert len(first_buttons) == 4
        assert first_buttons[-1]['text'] == 'Далее'
        scenario = await repository.get_active_for_customer('42')
        assert len(scenario.state['choices']) == 4
        page = await send(flow, database, callback=first_buttons[-1]['callback_data'])
        assert [item['text'] for item in buttons(page)][-1] == 'Назад'
        assert [item['index'] for item in json.loads(await flow.routing_context('42'))['choices']] == [3]
        assert flow._parse_callback(buttons(page)[0]['callback_data'])[2] == 3
        for item in buttons(page):
            assert len(item['callback_data'].encode()) <= 64
        if selection == 'button':
            reply = await send(flow, database, callback=buttons(page)[0]['callback_data'])
        else:
            reply = await send(flow, database, decision=RouteDecision(
                'booking_management', 1, 'continue', choice=3))
        if operation == 'reschedule':
            reply = await send(flow, database, callback=buttons(reply)[0]['callback_data'])
        assert adapter.cancel_calls == adapter.reschedule_calls == 0
        confirmation = buttons(reply)[0]['callback_data']
        await send(flow, database, callback=confirmation, update='confirm')
        await send(flow, database, callback=confirmation, update='confirm')
        await send(flow, database, callback=confirmation, update='replay')
        assert adapter.cancel_calls == (operation == 'cancel')
        assert adapter.reschedule_calls == (operation == 'reschedule')
        async with database.acquire() as connection:
            rows = await connection.fetch('SELECT external_id, status, slot_id FROM bookings')
        by_id = {row['external_id']: row for row in rows}
        for booking in original[:3]:
            assert by_id[booking.external_id]['status'] == 'confirmed'
            assert by_id[booking.external_id]['slot_id'] == booking.slot_id
        changed = by_id[original[3].external_id]
        assert changed['status'] == ('cancelled' if operation == 'cancel' else 'confirmed')
        assert changed['slot_id'] == (original[3].slot_id if operation == 'cancel' else 'slot-5')
    finally:
        await database.close()


async def test_pages_reject_stale_foreign_hidden_and_invalid_choices(migrated_database_url):
    database, repository, adapter, flow = await seeded(migrated_database_url)
    try:
        first = await send(flow, database, decision=RouteDecision('booking_management', 1, 'view'))
        next_callback = buttons(first)[-1]['callback_data']
        page = await send(flow, database, callback=next_callback)
        scenario = await repository.get_active_for_customer('42')
        assert scenario.state['booking_offset'] == 3
        for callback in [next_callback, buttons(first)[0]['callback_data'],
                         flow._callback(scenario, 'booking', 0),
                         flow._callback(scenario, 'booking', -1),
                         flow._callback(scenario, 'booking', 4),
                         flow._callback(scenario, 'booking_page', 6),
                         flow._callback(scenario, 'booking_page', 2)]:
            reply = await send(flow, database, callback=callback)
            assert STALE_REPLY in reply.text
            assert (await repository.get_active_for_customer('42')).state == scenario.state
        reply = await send(flow, database, callback=buttons(page)[0]['callback_data'], customer='other')
        assert STALE_REPLY in reply.text
        reply = await send(flow, database, decision=RouteDecision('booking_management', 1, 'continue', choice=0))
        assert STALE_REPLY in reply.text
        previous = await send(flow, database, callback=buttons(page)[-1]['callback_data'])
        assert [x['index'] for x in json.loads(await flow.routing_context('42'))['choices']] == [0, 1, 2]
        assert buttons(previous)[-1]['text'] == 'Далее'
        assert adapter.cancel_calls == adapter.reschedule_calls == 0
    finally:
        await database.close()


async def test_legacy_revision_and_callback_index_ceiling(migrated_database_url):
    database, repository, adapter, flow = await seeded(migrated_database_url)
    try:
        await send(flow, database, decision=RouteDecision('booking_management', 1, 'view'))
        scenario = await repository.get_active_for_customer('42')
        state = flow._state(scenario)
        state.pop('booking_offset', None)
        legacy = replace(scenario, state=state)
        view = {key: state.get(key) for key in (
            'step', 'choices', 'selected_slot_id', 'new_starts_at', 'selected_booking',
            'date', 'time_from', 'time_to')}
        assert flow._callback_revision(legacy) == hashlib.sha256(
            json.dumps(view, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
        assert flow._parse_callback(flow._callback(legacy, 'confirm_change', 0))[1] == 'confirm_change'
        assert ':5:' in flow._callback(legacy, 'confirm_change', 0)
        assert len(flow._callback(legacy, 'booking', MAX_CHOICE_INDEX).encode()) <= 64
        await repository.checkpoint(legacy, 'legacy_fixture')
        assert [x['index'] for x in json.loads(await flow.routing_context('42'))['choices']] == [0, 1, 2]
        reply = await send(flow, database, callback=buttons(flow._render_current(legacy))[-1]['callback_data'])
        assert flow._parse_callback(buttons(reply)[0]['callback_data'])[2] == 3
    finally:
        await database.close()


async def test_booking_limit_last_page_and_explicit_overflow(migrated_database_url):
    database, repository, adapter, flow = await seeded(migrated_database_url)
    try:
        # Bulk persisted fixture avoids 10,000 provider operations; all runtime reads are real.
        async with database.acquire() as connection:
            await connection.execute("""
                INSERT INTO bookings (id, last_scenario_id, external_id, customer_id,
                    slot_id, starts_at, status, snapshot, booking_key)
                SELECT gen_random_uuid(), b.last_scenario_id, 'bulk-' || n, '42',
                    b.slot_id, b.starts_at, b.status, b.snapshot, gen_random_uuid()
                FROM (SELECT * FROM bookings WHERE customer_id='42' LIMIT 1) b
                CROSS JOIN generate_series(1, 9996) n
            """)
        await send(flow, database, decision=RouteDecision('booking_management', 1, 'view'))
        scenario = await repository.get_active_for_customer('42')
        assert len(scenario.state['choices']) == 10000
        state = flow._state(scenario)
        state['booking_offset'] = 9996
        scenario = replace(scenario, state=state)
        await repository.checkpoint(scenario, 'last_page_fixture')
        page = await send(flow, database, callback=flow._callback(scenario, 'booking_page', 9999))
        assert [item['text'] for item in buttons(page)][-1] == 'Назад'
        assert [item['index'] for item in json.loads(await flow.routing_context('42'))['choices']] == [9999]
        assert all(len(item['callback_data'].encode()) <= 64 for item in buttons(page))
        await send(flow, database, decision=RouteDecision('booking', 1, 'cancel_draft'))
        async with database.acquire() as connection:
            await connection.execute("""
                INSERT INTO bookings (id, last_scenario_id, external_id, customer_id,
                    slot_id, starts_at, status, snapshot, booking_key)
                SELECT gen_random_uuid(), last_scenario_id, 'overflow', '42',
                    slot_id, starts_at, status, snapshot, gen_random_uuid()
                FROM bookings WHERE customer_id='42' LIMIT 1
            """)
        reply = await send(flow, database, update='overflow', decision=RouteDecision('booking_management', 1, 'view'))
        assert 'администратору' in reply.text
        assert not reply.delivery_options
        assert await repository.get_active_for_customer('42') is None
        assert adapter.cancel_calls == adapter.reschedule_calls == 0
    finally:
        await database.close()
