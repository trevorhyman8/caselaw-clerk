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

Return JSON: {{"intent": one of {intents}, "slot": "the digest number(s) this refers to, comma-separated if more than one (e.g. '1' or '1,3'), else null", "confidence": 0.0-1.0}}

Rules:
- A number, or a few numbers ("1", "1 and 3", "1, 3, 4") -> select_candidate with slot="1,3,4" (comma-separated, no spaces) — the user is picking one or several digest items to draft AT ONCE, ONLY if context says a digest is active
- "publish 1" / "publish" (when only one draft is ready) -> approve_publish with slot=the number if given, else null (this does NOT itself publish anything — a separate confirm step follows)
- A bare "yes" ONLY counts as confirm_yes if context says a publish confirmation is pending; otherwise it's unknown
- "edit 1: shorten the intro" / "shorten the intro" (when only one draft is ready) -> request_edit, slot = the number if given else null, and the FULL original message text is what gets used as the edit instruction (don't strip anything into a separate field)
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
