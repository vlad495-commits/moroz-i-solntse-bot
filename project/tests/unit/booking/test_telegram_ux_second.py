from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from moroz.booking.catalog import CatalogService, CatalogVariant
from moroz.booking.models import BookingScenario
from moroz.booking.telegram import TelegramBookingCoordinator

NOW = datetime(2026, 9, 5, tzinfo=UTC)


def service(name='Прессотерапия', category='Прессотерапия', sid='1', minutes=30):
    return CatalogService(sid, name, category, (CatalogVariant('10', 'Специалист', Decimal(900), Decimal(900), minutes),))


def scenario(**state):
    return BookingScenario(uuid4(), 'create', 'collecting', 'telegram:catalog:1', '42', state, None, NOW, NOW)


def coordinator(services):
    repo = AsyncMock()
    catalog = AsyncMock()
    catalog.list_services.return_value = tuple(services)
    return TelegramBookingCoordinator(repo, catalog, AsyncMock(), AsyncMock(), now=lambda: NOW)


def labels(reply):
    return [b['text'] for row in reply.delivery_options['reply_markup']['inline_keyboard'] for b in row]


@pytest.mark.asyncio
async def test_singleton_opens_informative_card_without_extra_page():
    c = coordinator([service()])
    reply = await c._catalog_choice(None, scenario(step='catalog_category'), 'catalog_category', {'category': 'Прессотерапия'})
    assert 'Страница' not in reply.text
    assert 'воздух' in reply.text.lower()
    assert labels(reply) == ['Выбрать время', 'Подробнее о процедуре', '← Категории']


@pytest.mark.asyncio
async def test_walkin_card_has_details_address_and_actual_duration_return():
    c = coordinator([service('КОЛЛАГЕНАРИЙ 6 минут', 'Загар', minutes=6)])
    reply = await c._catalog_choice(None, scenario(step='catalog_service', category='Загар', catalog_family='collagenarium'), 'catalog_service', {'service_id': '1'})
    assert 'без загара' in reply.text.lower()
    assert 'КОЛЛАГЕНАРИЙ' not in reply.text
    assert labels(reply) == ['Подробнее о процедуре', 'Как нас найти', '← Длительности']


def test_nine_categories_fit_single_screen():
    c = coordinator([])
    reply = c._render_current(scenario(step='catalog_category', choices=[{'label': str(i), 'category': str(i)} for i in range(9)]))
    assert labels(reply) == [str(i) for i in range(9)]


@pytest.mark.asyncio
async def test_hydrogen_variants_have_short_buttons_and_no_repeated_price():
    c = coordinator([service('Водородотерапия 30 минут', 'Водородотерапия'), service('Водородотерапия 60 минут', 'Водородотерапия', '2', 60)])
    reply = await c._catalog_choice(None, scenario(step='catalog_category'), 'catalog_category', {'category': 'Водородотерапия'})
    assert labels(reply)[:2] == ['30 минут', '60 минут']
    assert '₽' not in reply.text and 'Страница' not in reply.text


def test_booking_choices_exclude_walkin_and_normalize_display_only():
    raw = service('Комплекс " Премиум уход за лицом"')
    result = TelegramBookingCoordinator._service_choices((service('Солярий 5 минут'), raw))
    assert len(result) == 1
    assert result[0]['label'] == 'Комплекс «Премиум уход за лицом»'
    assert raw.service_name == 'Комплекс " Премиум уход за лицом"'


@pytest.mark.asyncio
async def test_short_question_uses_selected_service_without_llm():
    c = coordinator([service()])
    c._repository.get_active_for_customer.return_value = scenario(step='catalog_book', catalog_service_id='1', service_name='Прессотерапия')
    reply = await c.handle(None, customer_id='42', user_id='7', update_id='2', text='Что это?', kind='text', data={})
    assert reply is not None and 'воздух' in reply.text.lower()


@pytest.mark.asyncio
async def test_fresh_parts_question_does_not_claim_total_as_part_duration():
    c = coordinator([service('Fresh день', minutes=60)])
    c._repository.get_active_for_customer.return_value = scenario(step='catalog_book', catalog_service_id='1', service_name='Fresh день')
    reply = await c.handle(None, customer_id='42', user_id='7', update_id='2', text='На каждую процедуру сколько времени?', kind='text', data={})
    assert reply is not None and 'не подтверждена' in reply.text
    assert '60' not in reply.text


@pytest.mark.asyncio
@pytest.mark.parametrize("menu_label", ["🧭 Подобрать процедуру", "✨ Подобрать"])
async def test_selection_entry_is_deterministic_and_gift_has_url_actions(menu_label):
    c = coordinator([])
    c._repository.get_active_for_customer.return_value = None
    replies = [await c.handle(None, customer_id='42', user_id='7', update_id=str(i), text=menu_label, kind='text', data={}) for i in range(2)]
    assert replies[0] is not None and replies[0] == replies[1]
    markup = replies[0].delivery_options['reply_markup']
    assert [b['text'] for row in markup['keyboard'] for b in row][:4] == ['Отдых', 'Загар', 'Уход', 'Массаж']
    assert [b['text'] for b in markup['keyboard'][-1]] == ['🏷 Услуги и цены', '🗓 Записаться']
    gift = await c.handle(None, customer_id='42', user_id='7', update_id='3', text='Хочу подарочный сертификат', kind='text', data={})
    assert labels(gift) == ['Оформить сертификат', 'Уточнить у администратора']
    assert all('url' in b for row in gift.delivery_options['reply_markup']['inline_keyboard'] for b in row)


@pytest.mark.asyncio
async def test_duplicate_catalog_callback_is_silent_but_worker_retry_recovers():
    c = coordinator([service()])
    original = scenario(step='catalog_category', choices=[{'label': 'Прессотерапия', 'category': 'Прессотерапия'}])
    raw = c._callback(original, 'catalog_category', 0)
    current = replace(original, state={'step': 'catalog_book', 'catalog_service_id': '1', 'service_name': 'Прессотерапия', 'last_catalog_callback': raw, 'last_catalog_update_id': 'first'})
    c._repository.get_scenario.return_value = current
    c._repository.get_active_for_customer.return_value = current
    duplicate = await c._handle_callback(None, '42', '7', 'second', raw)
    assert duplicate.text == ''
    retry = await c._handle_callback(None, '42', '7', 'first', raw)
    assert 'Прессотерапия' in retry.text
    foreign = await c._handle_callback(None, 'other', '7', 'third', raw)
    assert foreign.text


def test_extended_fresh_program_keeps_its_own_composition():
    from moroz.booking.display import procedure_description
    assert 'общего массажа' in procedure_description('Fresh день расширенный')
    assert 'прессотерапии' in procedure_description('Fresh день')


@pytest.mark.asyncio
async def test_details_view_is_checkpointed_and_restored_on_retry():
    c = coordinator([service()])
    card = scenario(step='catalog_book', catalog_service_id='1', choices=[{'label': 'Подробнее о процедуре', 'service_id': '1', 'details': True}])
    raw = c._callback(card, 'catalog_book', 0)
    c._repository.get_scenario.return_value = card
    details = await c._handle_callback(None, '42', '7', 'first', raw)
    c._repository.checkpoint.assert_awaited()
    saved = c._repository.checkpoint.call_args.args[0]
    c._repository.get_scenario.return_value = saved
    assert (await c._handle_callback(None, '42', '7', 'second', raw)).text == ''
    assert (await c._handle_callback(None, '42', '7', 'first', raw)).text == details.text


@pytest.mark.asyncio
async def test_walkin_back_returns_to_selected_family_in_mixed_category():
    c = coordinator([service('Коллариум 6 минут', 'Загар', '1', 6), service('КОЛЛАГЕНАРИЙ 6 минут', 'Загар', '2', 6)])
    card = scenario(step='catalog_book', category='Загар', catalog_family='collagenarium', catalog_service_id='2')
    c._repository.get_scenario.return_value = card
    reply = await c._handle_callback(None, '42', '7', 'back', c._callback(card, 'catalog_back', 0))
    assert labels(reply) == ['6 мин · 900 ₽', '← Категории']


def test_gift_consultation_reply_gets_actions_for_free_wording():
    from moroz.messaging import telegram
    options = telegram.consultation_options('Можно купить подарочный сертификат?', 'Сертификат можно оформить онлайн: https://n1321481.yclients.com')
    assert [b['text'] for row in options['reply_markup']['inline_keyboard'] for b in row] == ['Оформить сертификат', 'Уточнить у администратора']
    assert telegram.consultation_options('Сертификат', 'Не могу помочь с этим запросом.') == {}


@pytest.mark.asyncio
async def test_each_part_duration_paraphrase_keeps_unknown_breakdown():
    c = coordinator([service('Fresh день', minutes=60)])
    c._repository.get_active_for_customer.return_value = scenario(step='catalog_book', catalog_service_id='1')
    reply = await c.handle(None, customer_id='42', user_id='7', update_id='parts', text='Сколько минут длится каждая часть Фреш дня?', kind='text', data={})
    assert reply is not None and 'не подтверждена' in reply.text
    assert '60' not in reply.text


def test_display_copy_handles_led_combo_and_lowercase_provider_name():
    from moroz.booking.display import procedure_description, service_display_name
    assert 'LED' in procedure_description('LED маска и Водородотерапия')
    assert service_display_name('коллариум 14 минут') == 'Коллариум 14 минут'


@pytest.mark.asyncio
async def test_old_repeated_callback_eventually_gets_recovery():
    from datetime import timedelta
    c = coordinator([service()])
    old = scenario(step='catalog_category', choices=[{'label': 'Прессотерапия', 'category': 'Прессотерапия'}])
    raw = c._callback(old, 'catalog_category', 0)
    current = replace(old, state={'step': 'catalog_book', 'catalog_service_id': '1', 'last_catalog_callback': raw, 'last_catalog_update_id': 'first'})
    c._repository.get_scenario.return_value = current
    c._repository.get_active_for_customer.return_value = current
    c._now = lambda: NOW + timedelta(minutes=2)
    assert (await c._handle_callback(None, '42', '7', 'later', raw)).text


def test_short_categories_pair_even_when_one_category_is_long():
    c = coordinator([])
    choices = [{'label': name, 'category': name} for name in ['Водородотерапия', 'Коллагенарий', 'Коллариум', 'Криотерапия', 'Массаж', 'Прессотерапия/Лимфодренажный массаж', 'Солярий', 'Уход за лицом', 'Фреш день']]
    reply = c._render_current(scenario(step='catalog_category', choices=choices))
    assert len(reply.delivery_options['reply_markup']['inline_keyboard']) <= 6
    assert len(labels(reply)) == 9
