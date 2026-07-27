from dataclasses import FrozenInstanceError

import pytest

from moroz.messaging.ingress import IngressDecision, decide_ingress


def test_nontext_is_a_local_reply_before_consent() -> None:
    assert decide_ingress(
        has_text=False,
        has_processing_consent=False,
    ) == IngressDecision("reply", "nontext")


def test_text_without_processing_consent_is_a_local_reply() -> None:
    assert decide_ingress(
        has_text=True,
        has_processing_consent=False,
    ) == IngressDecision("reply", "consent_required")


def test_consented_text_is_accepted() -> None:
    assert decide_ingress(
        has_text=True,
        has_processing_consent=True,
    ) == IngressDecision("accept", None)


def test_ingress_decision_is_immutable() -> None:
    decision = decide_ingress(
        has_text=False,
        has_processing_consent=False,
    )
    with pytest.raises(FrozenInstanceError):
        decision.action = "accept"  # type: ignore[misc]
