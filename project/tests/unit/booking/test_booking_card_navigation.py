from tests.unit.booking.test_telegram_ux_second import coordinator, scenario, service, labels
import pytest


def test_only_structural_booking_replies_mark_editable_card():
    c = coordinator([])
    draft = scenario(step='service', choices=[{'label': 'Прессотерапия', 'service_id': '1'}])
    assert c._render_current(draft).delivery_options['booking_card'] == str(draft.id)
    catalog = scenario(step='catalog_category', choices=[{'label': 'Услуги', 'category': 'Услуги'}])
    assert 'booking_card' not in c._render_current(catalog).delivery_options


@pytest.mark.asyncio
async def test_back_restores_previous_step_with_new_revision_and_same_card():
    c = coordinator([service()])
    original = scenario(step='service', choices=[c._service_choice(service())])
    old_button = c._callback(original, 'service', 0)
    c._repository.get_scenario.return_value = original
    c._port.list_slots.return_value = ()
    forward = await c._handle_callback(None, '42', '7', 'next', old_button)
    advanced = c._repository.checkpoint.call_args.args[0]
    assert 'Назад' in labels(forward)
    c._repository.get_scenario.return_value = advanced
    back = await c._handle_callback(None, '42', '7', 'back', c._callback(advanced, 'booking_back', 0))
    restored = c._repository.checkpoint.call_args.args[0]
    assert restored.state['step'] == 'service'
    assert back.delivery_options['booking_card'] == str(original.id)
    assert c._callback(restored, 'service', 0) != old_button
    c._repository.get_scenario.return_value = restored
    c._repository.get_active_for_customer.return_value = restored
    stale = await c._handle_callback(None, '42', '7', 'old', old_button)
    assert 'неактуальна' in stale.text
    assert c._repository.checkpoint.call_args.args[0].state['step'] == 'service'
    c._booking_service.handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_double_forward_does_not_advance_twice():
    c = coordinator([service()])
    original = scenario(step='service', choices=[c._service_choice(service())])
    raw = c._callback(original, 'service', 0)
    c._repository.get_scenario.return_value = original
    c._port.list_slots.return_value = ()
    first = await c._handle_callback(None, '42', '7', 'first', raw)
    advanced = c._repository.checkpoint.call_args.args[0]
    c._repository.get_scenario.return_value = advanced
    c._repository.get_active_for_customer.return_value = advanced
    repeated = await c._handle_callback(None, '42', '7', 'second', raw)
    assert repeated.delivery_options['booking_card'] == first.delivery_options['booking_card']
    assert 'неактуальна' in repeated.text
    assert c._repository.checkpoint.call_args.args[0].state['step'] == advanced.state['step']


@pytest.mark.asyncio
async def test_confirmation_is_editable_but_completion_is_permanent():
    from types import SimpleNamespace
    c = coordinator([service()])
    state = dict(step='contact', customer_phone='+79991234567', customer_name='Тест', service_name='Прессотерапия', staff_name='Специалист', starts_at='2026-09-06T12:00:00+03:00')
    draft = scenario(**state)
    summary = await c._show_confirmation(draft, state)
    assert summary.delivery_options['booking_card'] == str(draft.id)
    waiting = c._repository.checkpoint.call_args.args[0]
    c._repository.get_scenario.return_value = waiting
    c._booking_service.handle.return_value = SimpleNamespace(next_action=None, message='Запись подтверждена')
    result = await c._handle_callback(None, '42', '7', 'confirm', c._callback(waiting, 'confirm', 0))
    assert 'booking_card' not in result.delivery_options
    assert result.text == 'Запись подтверждена'


@pytest.mark.asyncio
async def test_date_time_forward_and_back_keep_card_and_reject_old_revision():
    from datetime import timedelta
    from moroz.booking.models import Slot
    from tests.unit.booking.test_telegram_ux_second import NOW
    c = coordinator([service()])
    c._port.list_slots.return_value = (Slot('slot1', ('1',), '10', NOW + timedelta(days=1, hours=10), 30),)
    start = scenario(step='service', choices=[c._service_choice(service())])
    c._repository.get_scenario.return_value = start
    dates = await c._handle_callback(None, '42', '7', 'service', c._callback(start, 'service', 0))
    day = c._repository.checkpoint.call_args.args[0]
    assert day.state['step'] == 'available_date'
    c._repository.get_scenario.return_value = day
    old_date = c._callback(day, 'available_date', 0)
    times = await c._handle_callback(None, '42', '7', 'date', old_date)
    time = c._repository.checkpoint.call_args.args[0]
    assert time.state['step'] == 'slot'
    c._repository.get_scenario.return_value = time
    back = await c._handle_callback(None, '42', '7', 'back', c._callback(time, 'booking_back', 0))
    restored = c._repository.checkpoint.call_args.args[0]
    assert restored.state['step'] == 'available_date'
    assert c._callback(restored, 'available_date', 0) != old_date
    assert dates.delivery_options['booking_card'] == times.delivery_options['booking_card'] == back.delivery_options['booking_card']


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['confirm', 'confirm_change'])
async def test_confirmation_retry_still_reaches_idempotent_service(action):
    from dataclasses import replace
    from types import SimpleNamespace
    c = coordinator([])
    waiting = replace(scenario(step=action, new_starts_at='2026-09-06T12:00:00+03:00'), phase='awaiting_confirmation', kind='reschedule' if action == 'confirm_change' else 'create')
    raw = c._callback(waiting, action, 0)
    c._repository.get_scenario.return_value = waiting
    c._booking_service.handle.side_effect = [RuntimeError('transient service failure'), SimpleNamespace(next_action=None, message='Готово')]
    with pytest.raises(RuntimeError):
        await c._handle_callback(None, '42', '7', 'same-update', raw)
    saved = c._repository.checkpoint.call_args.args[0]
    c._repository.get_scenario.return_value = saved
    c._repository.get_active_for_customer.return_value = saved
    result = await c._handle_callback(None, '42', '7', 'same-update', raw)
    assert c._booking_service.handle.await_count == 2
    assert result.text == 'Готово'
    assert 'booking_card' not in result.delivery_options


@pytest.mark.asyncio
@pytest.mark.parametrize('step', ['service', 'available_date'])
async def test_text_selection_preserves_previous_visible_step(step):
    from moroz.messaging.router import RouteDecision
    from moroz.booking.models import Slot
    from datetime import timedelta
    from tests.unit.booking.test_telegram_ux_second import NOW
    c = coordinator([service()])
    c._port.list_slots.return_value = (Slot('slot1', ('1',), '10', NOW + timedelta(days=1, hours=10), 30),)
    state = dict(step=step, choices=[c._service_choice(service())] if step == 'service' else [{'date': '2026-09-06', 'label': '06.09'}])
    if step != 'service':
        state.update(service_id='1', service_name='Прессотерапия', staff_names={'10': 'Специалист'}, staff_id='10', staff_name='Специалист')
    shown = scenario(**state)
    c._repository.get_active_for_customer.return_value = shown
    decision = RouteDecision('booking', .99, 'continue', service='Прессотерапия') if step == 'service' else RouteDecision('booking', .99, 'continue', date='2026-09-06')
    reply = await c.handle(None, customer_id='42', user_id='7', update_id='text', text='Прессотерапия' if step == 'service' else '6 сентября', kind='text', data={}, decision=decision)
    saved = c._repository.checkpoint.call_args.args[0]
    assert saved.state['booking_history'][-1]['step'] == step
    assert 'Назад' in labels(reply)
