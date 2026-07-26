from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class IngressDecision:
    action: Literal["accept", "reply"]
    code: Literal["nontext", "consent_required"] | None


def decide_ingress(
    *,
    has_text: bool,
    has_processing_consent: bool,
) -> IngressDecision:
    if not has_text:
        return IngressDecision("reply", "nontext")
    if not has_processing_consent:
        return IngressDecision("reply", "consent_required")
    return IngressDecision("accept", None)
