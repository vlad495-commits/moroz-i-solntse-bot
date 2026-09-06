import pytest

from moroz.messaging.router import RouteDecision, RouterVerdict
from moroz.security.llm_gateway import LLMUnavailable, NonRetryableLLMError
from moroz.security.pipeline import SAFE_OUTPUT_FALLBACK
from tests.unit.security.test_pipeline import CapturingGateway, CapturingRouter, pipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [LLMUnavailable, NonRetryableLLMError])
@pytest.mark.parametrize("topic", ["price", "preparation"])
async def test_answer_outage_preserves_dispatch_once(error, topic):
    gateway = CapturingGateway(error())
    router = CapturingRouter(RouterVerdict(RouteDecision(
        "booking", 0.99, "continue", topics=(topic,),
    )))
    calls = []
    local_reply = "Проверьте данные записи и подтвердите выбранное время."

    async def dispatch(decision):
        calls.append(decision)
        return local_reply

    result = await pipeline(gateway, router=router).respond(
        "И сколько стоит процедура?", [], dispatch=dispatch
    )
    assert result.text == SAFE_OUTPUT_FALLBACK + "\n\n" + local_reply
    assert len(calls) == 1
    assert [request.purpose for request in gateway.requests] == ["security", "answer"]
