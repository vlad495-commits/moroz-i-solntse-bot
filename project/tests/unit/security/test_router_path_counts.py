"""Characterize pipeline calls, not the quality of live Router classification."""

from collections import Counter
import json
import logging

import pytest

from moroz.messaging.router import LLMIntentRouter
from moroz.security.input_security import LLMInputSecurityClassifier
from moroz.security.output_validator import LLMOutputValidator
from moroz.security.pipeline import SecurityPipeline
from moroz.security.validator import extract_structured_facts
from tests.unit.security.test_pipeline import CapturingGateway, response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,fields,active,needs_answer",
    [
        ("Сколько стоит криокапсула?", {"route": "consultation", "topics": ["price"]}, False, True),
        ("Сколько стоит и запишите на криокапсулу", {"topics": ["price"]}, False, True),
        ("Запишите на криокапсулу", {}, False, False),
        ("Лучше к Марии после 18:00", {"action": "continue", "staff": "Мария", "time_from": "18:00"}, True, False),
        ("7 сентября", {"action": "continue", "date": "2026-09-07"}, True, False),
        ("7 сентября", {"route": "booking_management", "action": "continue", "date": "2026-09-07"}, True, False),
        ("Как подготовиться?", {"route": "consultation", "topics": ["preparation"]}, True, True),
    ],
    ids=["faq", "price-and-booking", "booking", "preferences", "date-only", "reschedule", "faq-in-draft"],
)
async def test_router_path_provider_counts(text, fields, active, needs_answer, caplog):
    payload = {
        "route": "booking", "action": "create", "confidence": 0.99,
        "services": ["Криокапсула"], "topics": [], "date": None,
        "time_from": None, "time_to": None, "staff": None, "choice": None,
        **fields,
    }
    if payload["route"] == "consultation":
        payload["action"] = "none"
    prompt = "Криокапсула — 2 400 ₽."
    events = [response(json.dumps(payload), purpose="router")]
    if needs_answer:
        events.extend([
            response(prompt),
            response('{"action":"allow","category":"safe"}', purpose="validator"),
        ])
    provider = CapturingGateway(*events)
    pipeline = SecurityPipeline(
        provider, prompt, extract_structured_facts(prompt),
        router=LLMIntentRouter(provider),
        input_security=LLMInputSecurityClassifier(provider),
        output_validator=LLMOutputValidator(provider),
    )
    dispatched = []
    local_reply = "Выберите удобное время."

    async def dispatch(decision):
        dispatched.append(decision)
        if decision.route in {"booking", "booking_management"}:
            return local_reply
        return None

    state = json.dumps({
        "active": active, "today": "2026-09-06",
        "kind": "reschedule" if payload["route"] == "booking_management" else "create",
    })
    result = await pipeline.respond(text, [], dispatch=dispatch, booking_context=state)

    expected = Counter(security=1, router=1)
    if needs_answer:
        expected.update(answer=1, validator=1)
    assert Counter(request.purpose for request in provider.requests) == expected
    assert not provider.events
    assert len(dispatched) == 1
    assert dispatched[0].route == payload["route"]
    assert dispatched[0].action == payload["action"]
    assert dispatched[0].date == payload["date"]
    assert dispatched[0].staff == payload["staff"]
    assert dispatched[0].time_from == payload["time_from"]
    expected_text = prompt if needs_answer else ""
    if payload["route"] in {"booking", "booking_management"}:
        expected_text = f"{expected_text}\n\n{local_reply}" if expected_text else local_reply
    assert result.text == expected_text
    assert not [
        record for record in caplog.records
        if record.name.startswith("moroz.security") and record.levelno >= logging.WARNING
    ]
