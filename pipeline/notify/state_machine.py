"""Conversation state machine — pure logic, no Telegram dependency, so it's
unit-testable without a live bot token (which is a backloaded operator
step). `telegram_bot.py` is a thin wrapper that feeds inbound text into
`handle()` and executes whatever `Action` comes back.

Transitions are only legal as drawn (plan §Telegram loop):
  IDLE -> DIGEST_SENT -> DRAFTING -> PREVIEW_SENT -> CONFIRM_PUBLISH -> PUBLISHED
                              ^            |
                              |            v
                           REVISING <------+
  any gate failure x2 -> NEEDS_REVIEW (human, never auto-publishable)
  PREVIEW_SENT -> DISCARDED (reject/skip)

PUBLISHED is reachable ONLY from CONFIRM_PUBLISH, which is reachable ONLY
from a PREVIEW_SENT whose stored draft has gate_status == "pass" (or "warn"
— never "needs_review"). The action executor re-checks the draft's content
hash at execution time so a stale confirm after an edit can't slip through.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum


class State(str, Enum):
    IDLE = "idle"
    DIGEST_SENT = "digest_sent"
    DRAFTING = "drafting"
    PREVIEW_SENT = "preview_sent"
    REVISING = "revising"
    CONFIRM_PUBLISH = "confirm_publish"
    PUBLISHED = "published"
    NEEDS_REVIEW = "needs_review"
    DISCARDED = "discarded"


@dataclass
class Conversation:
    telegram_user_id: str
    state: State = State.IDLE
    digest_items: list[dict] = field(default_factory=list)  # [{case_id, rank, hook}]
    active_case_id: str | None = None
    active_draft_id: str | None = None
    active_draft_content_hash: str | None = None
    confirm_publish_expires_at: datetime | None = None
    edit_rounds: int = 0
    max_edit_rounds: int = 3
    confirm_timeout_minutes: int = 10


@dataclass
class Action:
    kind: str  # send_message | start_draft | start_revision | do_publish | noop
    text: str | None = None
    case_id: str | None = None
    draft_id: str | None = None
    edit_instruction: str | None = None


CANDIDATE_REF_MAP_HELP = (
    "Reply a number to draft, 'all' for all, or 'skip'."
)


def send_digest(conv: Conversation, digest_items: list[dict]) -> Action:
    conv.digest_items = digest_items
    conv.state = State.DIGEST_SENT
    lines = ["Good morning. " + (
        f"{len(digest_items)} new decision(s) worth a look:" if digest_items
        else "Nothing new worth a post today."
    )]
    for item in digest_items:
        lines.append(f"{item['rank']}️⃣ {item['hook']}")
    if digest_items:
        lines.append(CANDIDATE_REF_MAP_HELP)
    return Action("send_message", text="\n".join(lines))


def handle(conv: Conversation, intent: str, slot: str | None, raw_text: str) -> Action:
    """Pure transition function. Returns the Action for the caller to
    execute; does NOT itself call the draft engine, gate, or WordPress —
    keeping this importable/testable with zero network or subprocess deps."""

    if intent == "status":
        return Action("send_message", text=f"State: {conv.state.value}")

    if conv.state == State.DIGEST_SENT:
        if intent == "select_candidate" and slot:
            item = _resolve_candidate(conv, slot)
            if not item:
                return Action("send_message", text=f"Didn't recognize '{slot}' — {CANDIDATE_REF_MAP_HELP}")
            conv.active_case_id = item["case_id"]
            conv.state = State.DRAFTING
            return Action("start_draft", case_id=item["case_id"], text=f"On it — drafting {item['hook']}. ~90 seconds.")
        if intent == "draft_all":
            conv.state = State.DRAFTING
            return Action("start_draft", case_id="__all__", text="Drafting all candidates.")
        if intent == "reject" or raw_text.strip().lower() == "skip":
            conv.state = State.IDLE
            return Action("send_message", text="OK — I'll include these in Saturday's recap if you want to revisit.")
        return Action("send_message", text=f"Not sure what you mean — {CANDIDATE_REF_MAP_HELP}")

    if conv.state == State.PREVIEW_SENT:
        if intent == "approve_publish":
            conv.state = State.CONFIRM_PUBLISH
            conv.confirm_publish_expires_at = datetime.now(timezone.utc) + timedelta(
                minutes=conv.confirm_timeout_minutes
            )
            return Action(
                "send_message",
                text="Publishing to consumerfinanceprivacycounsel.com. Reply YES to confirm.",
            )
        if intent == "request_edit":
            if conv.edit_rounds >= conv.max_edit_rounds:
                return Action(
                    "send_message",
                    text=f"That's {conv.max_edit_rounds} edit rounds already — let me know if you want to start fresh, or just publish/skip.",
                )
            conv.edit_rounds += 1
            conv.state = State.REVISING
            return Action("start_revision", draft_id=conv.active_draft_id, edit_instruction=raw_text)
        if intent == "reject":
            conv.state = State.DISCARDED
            return Action("send_message", text="Discarded — I'll include it in Saturday's recap for 14 days in case you change your mind.")
        return Action(
            "send_message",
            text="Reply 'publish', tell me what to change, or 'skip'.",
        )

    if conv.state == State.CONFIRM_PUBLISH:
        if conv.confirm_publish_expires_at and datetime.now(timezone.utc) > conv.confirm_publish_expires_at:
            conv.state = State.PREVIEW_SENT
            return Action("send_message", text="Confirmation timed out — reply 'publish' again if you still want to.")
        if intent == "confirm_yes" and raw_text.strip().lower() == "yes":
            conv.state = State.PUBLISHED
            return Action("do_publish", draft_id=conv.active_draft_id)
        conv.state = State.PREVIEW_SENT
        return Action("send_message", text="Not published — reply 'publish' again if you'd like to.")

    if conv.state in (State.DRAFTING, State.REVISING):
        return Action("send_message", text="Still working on it — I'll message you when it's ready.")

    if conv.state in (State.PUBLISHED, State.DISCARDED, State.NEEDS_REVIEW):
        return Action(
            "send_message",
            text="That one's already wrapped up. Say 'status' or wait for tomorrow's digest.",
        )

    return Action("send_message", text="No digest is active right now. Check back at the next morning digest, or say 'recap' to see the past week.")


def draft_ready(conv: Conversation, draft_id: str, content_hash: str, report_card: str, preview_url: str) -> Action:
    conv.active_draft_id = draft_id
    conv.active_draft_content_hash = content_hash
    conv.state = State.PREVIEW_SENT
    conv.edit_rounds = 0
    text = f"{report_card}\n\nPreview: {preview_url}\nReply 'publish', or tell me what to change."
    return Action("send_message", text=text)


def draft_needs_review(conv: Conversation, reasons: list[str]) -> Action:
    conv.state = State.NEEDS_REVIEW
    text = "This one didn't clear verification and needs a human look:\n" + "\n".join(f"- {r}" for r in reasons)
    return Action("send_message", text=text)


def _resolve_candidate(conv: Conversation, slot: str) -> dict | None:
    for item in conv.digest_items:
        if str(item["rank"]) == str(slot).strip():
            return item
    return None
