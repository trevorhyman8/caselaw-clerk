"""Inbound-message intent classification. Closed enum, low-confidence
always routes to "ask, don't act" — this parser NEVER triggers a publish by
itself; publish requires the separate two-step typed-confirm flow in
state_machine.py regardless of what this returns.
"""
from __future__ import annotations

from dataclasses import dataclass

from pipeline import llm

INTENT_ENUM = [
    "select_candidate", "draft_all", "request_edit", "approve_publish",
    "confirm_yes", "reject", "question", "snooze", "add_category",
    "recap_request", "status", "pause", "unknown",
]

PROMPT = """You are parsing a text message sent to a legal-blog drafting assistant by its one authorized user, a litigation partner. Classify the message's intent.

Message: "{text}"

Context: {context}

Return JSON: {{"intent": one of {intents}, "slot": "candidate number or reference if applicable, else null", "confidence": 0.0-1.0}}

Rules:
- "publish" or "yes, publish it" -> approve_publish (this does NOT itself publish anything — a separate confirm step follows)
- A bare "yes" ONLY counts as confirm_yes if context says a publish confirmation is pending; otherwise it's unknown
- A number like "1" or "2" -> select_candidate with that slot, ONLY if context says a digest is active
- Free-text instructions like "shorten the intro", "lead with the holding" -> request_edit
- If you are not confident, return intent="unknown" with confidence below 0.8 — never guess on anything publish-adjacent"""


@dataclass
class ParsedIntent:
    intent: str
    slot: str | None
    confidence: float

    @property
    def actionable(self) -> bool:
        return self.confidence >= 0.8 and self.intent != "unknown"


def parse(text: str, context: str) -> ParsedIntent:
    data = llm.complete_json(PROMPT.format(text=text, context=context, intents=INTENT_ENUM))
    return ParsedIntent(
        intent=data.get("intent", "unknown"),
        slot=data.get("slot"),
        confidence=float(data.get("confidence", 0.0)),
    )
