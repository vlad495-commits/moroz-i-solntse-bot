import pytest

from moroz.messaging.router import RouteDecision, RouterVerdict, deterministic_route
from moroz.security.pipeline import SecurityPipeline
from moroz.security.llm_gateway import LLMResponse
from moroz.security.input_security import InputSecurityDecision, InputSecurityVerdict
from moroz.security.validator import extract_structured_facts
from moroz.booking.catalog import CatalogGrounding, CatalogService, CatalogVariant
from decimal import Decimal


class Gateway:
    async def complete(self, request):
        raise AssertionError('Booking must dispatch without generating an answer')


class Security:
    async def classify(self, text):
        return InputSecurityVerdict(InputSecurityDecision('allow', 'llm', 'ok'))


class Router:
    def __init__(self, source='llm'):
        self.calls = []
        self.source = source

    async def route(self, text, context, *, state=None):
        self.calls.append((text, context))
        return RouterVerdict(RouteDecision('booking', .98), source=self.source)


@pytest.mark.asyncio
async def test_free_text_routes_once_then_dispatches_after_security():
    router = Router()
    seen = []

    async def dispatch(decision):
        seen.append(decision.route)
        return 'Выберите услугу'

    result = await SecurityPipeline(Gateway(), '', extract_structured_facts(''),
                                    router=router, input_security=Security()).respond(
        'Хочу записаться', [], dispatch=dispatch)
    assert result.text == 'Выберите услугу'
    assert seen == ['booking']
    assert len(router.calls) == 1


@pytest.mark.parametrize('text', ['Хочу записаться', 'Отмени запись', 'Спасибо', 'Позови администратора'])
def test_human_text_has_no_keyword_route(text):
    assert deterministic_route(text) is None


@pytest.mark.asyncio
async def test_router_failure_never_dispatches_a_booking():
    async def forbidden(decision):
        raise AssertionError('failed router must not dispatch')

    result = await SecurityPipeline(Gateway(), '', extract_structured_facts(''),
                                    router=Router('fallback'), input_security=Security()).respond(
        'Хочу записаться', [], dispatch=forbidden)
    assert 'кноп' in result.text


@pytest.mark.asyncio
async def test_security_block_prevents_booking_dispatch():
    class Blocking:
        async def classify(self, text):
            return InputSecurityVerdict(InputSecurityDecision('block', 'llm', 'block'))

    async def forbidden(decision):
        raise AssertionError('blocked input must not mutate a scenario')

    result = await SecurityPipeline(Gateway(), '', extract_structured_facts(''),
        router=Router(), input_security=Blocking()).respond('Хочу записаться', [], dispatch=forbidden)
    assert result.model == 'security-llm'


@pytest.mark.asyncio
async def test_catalog_resolves_followup_after_semantic_router():
    class PriceRouter:
        async def route(self, text, context, *, state=None):
            assert context[0]['content'] == 'Расскажи про криомассаж головы'
            return RouterVerdict(RouteDecision('consultation', .99, service='Криомассаж головы'))

    seen = []
    async def catalog(decision):
        seen.append(decision.service)
        return CatalogGrounding('fresh', (CatalogService('1', 'Криомассаж головы', 'Крио',
            (CatalogVariant('10', 'Анна', Decimal(1500), Decimal(1500), 30),)),), 'price', False)

    result = await SecurityPipeline(Gateway(), '', extract_structured_facts(''),
        router=PriceRouter(), input_security=Security()).respond('Сколько стоит?',
            [{'role': 'user', 'content': 'Расскажи про криомассаж головы'}], catalog=catalog)
    assert seen == ['Криомассаж головы']
    assert '1 500 ₽' in result.text


@pytest.mark.asyncio
async def test_failed_router_does_not_load_catalog():
    async def forbidden(decision):
        raise AssertionError('catalog must not load on failed router')
    result = await SecurityPipeline(Gateway(), '', extract_structured_facts(''),
        router=Router('fallback'), input_security=Security()).respond('Сколько стоит?', [], catalog=forbidden)
    assert result.model == 'router-fallback'
