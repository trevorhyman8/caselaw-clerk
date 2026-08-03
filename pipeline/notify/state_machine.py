"""Conversation state machine — pure logic, no Telegram dependency, so it's
unit-testable without a live bot token (which is a backloaded operator
step). `telegram_bot.py` is a thin wrapper that feeds inbound text into
`handle()` and executes whatever `Action` comes back.

Real workflow this is built for (clarified after live testing): Scott
checks the digest on his phone in the car, picks one or a FEW cases, and
has ~20 minutes before he's at his desk — plenty of time even at several
minutes per draft, as long as picking more than one case doesn't serialize.
So drafting is batch-capable: reply with one number, a few ("1, 3"), or
"all", and every selected case drafts CONCURRENTLY (see
telegram_bot.py::_run_batch_and_reply, which fans them out via a thread
pool since run_draft_pipeline is a blocking call). Each report card is
sent as soon as ITS draft is ready — Scott doesn't wait for the slowest one
to see the fastest.

Transitions:
  IDLE -> DIGEST_SENT -> DRAFTING -> PREVIEW_SENT -> CONFIRM_PUBLISH -> PUBLISHED
                              ^            |
                              |            v
                           REVISING <------+
  any gate failure x2 -> NEEDS_REVIEW (human, never auto-publishable)
  PREVIEW_SENT -> DISCARDED (reject/skip)

With multiple drafts in flight, each is tracked by its digest RANK (the
same 1️⃣2️⃣3️⃣ number Scott used to pick it) — `pending_ranks` while drafting,
`ready[rank]` once a report card exists. DRAFTING -> PREVIEW_SENT fires once
`pending_ranks` is empty (every selected case has resolved, ready OR
needs_review) and at least one draft is ready to act on.

PUBLISHED is reachable ONLY from CONFIRM_PUBLISH, which requires a specific
ready rank with gate_status in (pass, warn) — never needs_review. The
action executor re-checks that draft's content hash at execution time so a
stale confirm after an edit can't slip through.
"""
from __future__ import annotations

import re
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
    pending_ranks: set[int] = field(default_factory=set)
    # rank -> {draft_id, content_hash, hook, gate_status, reasons}. Holds
    # BOTH publishable (pass/warn) and needs_review drafts — unified so
    # "edit 4" can find and re-draft a needs_review item just as easily as
    # a clean one; only the publish path discriminates on gate_status.
    ready: dict[int, dict] = field(default_factory=dict)
    published_ranks: set[int] = field(default_factory=set)
    confirm_rank: int | None = None
    confirm_publish_expires_at: datetime | None = None
    edit_rounds: dict[int, int] = field(default_factory=dict)
    max_edit_rounds: int = 3
    confirm_timeout_minutes: int = 10


@dataclass
class Action:
    kind: str  # send_message | start_draft | start_revision | do_publish | noop
    text: str | None = None
    case_ids: list[str] | None = None  # start_draft: one or more, drafted concurrently
    ranks: list[int] | None = None      # parallel to case_ids, same order
    draft_id: str | None = None
    content_hash: str | None = None    # do_publish: captured at pop-time, since conv.ready
                                        # is mutated synchronously before the executor runs
    edit_instruction: str | None = None


CANDIDATE_REF_MAP_HELP = (
    "Reply a number (or a few, like '1, 3') to draft, 'all' for all, or 'skip'."
)

_RANK_RE = re.compile(r"\d+")


def _is_actionable(item: dict) -> bool:
    """Single source of truth for "can this rank be published as-is" —
    used everywhere that decision matters (approve_publish gating,
    has-anything-ready state transitions, the ready-list hint message) so
    they can't drift out of sync with each other."""
    return item.get("gate_status") in ("pass", "warn")


def has_actionable(conv: Conversation) -> bool:
    return any(_is_actionable(v) for v in conv.ready.values())


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


def _parse_ranks(slot: str) -> list[int]:
    return sorted({int(n) for n in _RANK_RE.findall(slot or "")})


def _resolve_candidates(conv: Conversation, slot: str) -> list[dict]:
    ranks = _parse_ranks(slot)
    by_rank = {item["rank"]: item for item in conv.digest_items}
    return [by_rank[r] for r in ranks if r in by_rank]


def _start_batch(conv: Conversation, items: list[dict]) -> Action:
    conv.pending_ranks = {item["rank"] for item in items}
    conv.ready = {}
    conv.state = State.DRAFTING
    if len(items) == 1:
        text = f"On it — drafting {items[0]['hook']}. I'll message you when it's ready."
    else:
        hooks = "\n".join(f"  {i['rank']}️⃣ {i['hook']}" for i in items)
        text = f"On it — drafting {len(items)} cases concurrently:\n{hooks}\nI'll send each as it's ready."
    return Action(
        "start_draft", text=text,
        case_ids=[i["case_id"] for i in items], ranks=[i["rank"] for i in items],
    )


def handle(conv: Conversation, intent: str, slot: str | None, raw_text: str) -> Action:
    """Pure transition function. Returns the Action for the caller to
    execute; does NOT itself call the draft engine, gate, or WordPress —
    keeping this importable/testable with zero network or subprocess deps."""

    if intent == "status":
        pending = f"pending: {sorted(conv.pending_ranks)}" if conv.pending_ranks else ""
        readyd = f"ready: {sorted(conv.ready)}" if conv.ready else ""
        return Action("send_message", text=f"State: {conv.state.value}. {pending} {readyd}".strip())

    if conv.state == State.DIGEST_SENT:
        if intent == "select_candidate" and slot:
            items = _resolve_candidates(conv, slot)
            if not items:
                return Action("send_message", text=f"Didn't recognize '{slot}' — {CANDIDATE_REF_MAP_HELP}")
            return _start_batch(conv, items)
        if intent == "draft_all":
            if not conv.digest_items:
                return Action("send_message", text="Nothing in today's digest to draft.")
            return _start_batch(conv, conv.digest_items)
        if intent == "reject" or raw_text.strip().lower() == "skip":
            conv.state = State.IDLE
            return Action("send_message", text="OK — I'll include these in Saturday's recap if you want to revisit.")
        return Action("send_message", text=f"Not sure what you mean — {CANDIDATE_REF_MAP_HELP}")

    if conv.state in (State.DRAFTING, State.PREVIEW_SENT, State.NEEDS_REVIEW):
        # Even while some cases are still drafting, Scott can act on ones
        # that are already ready (e.g. "publish 1" while 3 is still cooking).
        if intent == "select_candidate" and slot:
            # A rank can reach here already fully resolved (a hard pipeline
            # exception removes it from both pending_ranks and ready — see
            # telegram_bot.py's exception handler), and the bot explicitly
            # tells Scott to reply the number again in that case. Honor
            # that: retry it as a fresh single-item batch. Ranks still
            # in-flight or already showing a result are NOT retriable this
            # way — this only fires for a rank that's genuinely gone.
            items = _resolve_candidates(conv, slot)
            retriable = [
                i for i in items
                if i["rank"] not in conv.pending_ranks and i["rank"] not in conv.ready
            ]
            if retriable:
                # Adds to pending_ranks WITHOUT touching conv.ready — unlike
                # _start_batch (used only from DIGEST_SENT, where nothing
                # else is in flight yet), a retry here must not clobber
                # other ranks from the same original batch that already
                # succeeded or are still drafting.
                conv.pending_ranks |= {i["rank"] for i in retriable}
                conv.state = State.DRAFTING
                hooks = "\n".join(f"  {i['rank']}️⃣ {i['hook']}" for i in retriable)
                return Action(
                    "start_draft",
                    text=f"Retrying:\n{hooks}" if len(retriable) > 1 else f"Retrying {retriable[0]['hook']}.",
                    case_ids=[i["case_id"] for i in retriable], ranks=[i["rank"] for i in retriable],
                )
        if intent == "approve_publish":
            rank = _target_rank(conv, slot)
            if rank is None:
                return Action("send_message", text=_ambiguous_rank_message(conv, "publish"))
            if not _is_actionable(conv.ready[rank]):
                return Action(
                    "send_message",
                    text=f"{rank}️⃣ hasn't cleared verification yet, so it can't be published. "
                         f"Reply 'edit {rank}: ...' to fix it, or handle it manually in WordPress.",
                )
            conv.confirm_rank = rank
            conv.state = State.CONFIRM_PUBLISH
            title = conv.ready[rank].get("hook", f"case {rank}")
            return Action(
                "send_message",
                text=f"Publishing \"{title}\" to consumerfinanceprivacycounsel.com. Reply YES to confirm.",
            )
        if intent == "request_edit":
            rank = _target_rank(conv, slot)
            if rank is None:
                return Action("send_message", text=_ambiguous_rank_message(conv, "edit"))
            if conv.edit_rounds.get(rank, 0) >= conv.max_edit_rounds:
                return Action(
                    "send_message",
                    text=f"That's {conv.max_edit_rounds} edit rounds on that one already.",
                )
            conv.edit_rounds[rank] = conv.edit_rounds.get(rank, 0) + 1
            conv.pending_ranks.add(rank)
            item = conv.ready.pop(rank, None)
            conv.state = State.DRAFTING if conv.pending_ranks else conv.state
            return Action(
                "start_revision", ranks=[rank],
                draft_id=item["draft_id"] if item else None,
                edit_instruction=raw_text,
            )
        if intent == "reject":
            rank = _target_rank(conv, slot)
            if rank is not None:
                conv.ready.pop(rank, None)
                # Recompute state from what's actually left — before this
                # fix, rejecting the last item from NEEDS_REVIEW left state
                # stuck there with nothing left to act on (a dead-end that
                # then fell through to a "publish/edit/skip" prompt naming
                # actions with nothing to apply them to).
                if conv.pending_ranks:
                    conv.state = State.DRAFTING
                elif has_actionable(conv):
                    conv.state = State.PREVIEW_SENT
                elif conv.ready:
                    conv.state = State.NEEDS_REVIEW
                else:
                    conv.state = State.IDLE
                return Action("send_message", text=f"Discarded {rank}️⃣.")
        if conv.state == State.DRAFTING and not conv.ready:
            return Action("send_message", text="Still working on it — I'll message you as each is ready.")
        return Action(
            "send_message",
            text="Reply 'publish N', tell me what to change (e.g. 'edit 1: shorten the intro'), or 'skip'.",
        )

    if conv.state == State.CONFIRM_PUBLISH:
        if conv.confirm_publish_expires_at and datetime.now(timezone.utc) > conv.confirm_publish_expires_at:
            conv.state = State.PREVIEW_SENT
            conv.confirm_rank = None
            return Action("send_message", text="Confirmation timed out — reply 'publish N' again if you still want to.")
        if intent == "confirm_yes" and raw_text.strip().lower() == "yes" and conv.confirm_rank is not None:
            rank = conv.confirm_rank
            popped = conv.ready.pop(rank)
            conv.published_ranks.add(rank)
            conv.confirm_rank = None
            # Publishing one rank doesn't finish the conversation if others
            # are still pending or ready — only go fully PUBLISHED when
            # this was the last thing left to act on.
            if conv.pending_ranks or conv.ready:
                conv.state = State.DRAFTING if conv.pending_ranks else State.PREVIEW_SENT
            else:
                conv.state = State.PUBLISHED
            return Action(
                "do_publish", draft_id=popped["draft_id"],
                content_hash=popped["content_hash"], ranks=[rank],
            )
        conv.state = State.PREVIEW_SENT
        return Action("send_message", text="Not published — reply 'publish N' again if you'd like to.")

    if conv.state in (State.PUBLISHED, State.DISCARDED):
        return Action(
            "send_message",
            text="That one's already wrapped up. Say 'status' or wait for tomorrow's digest.",
        )

    return Action("send_message", text="No digest is active right now. Check back at the next morning digest, or say 'recap' to see the past week.")


def draft_ready(
    conv: Conversation, rank: int, draft_id: str, content_hash: str,
    report_card: str, draft_text: str, hook: str, gate_status: str = "pass",
) -> Action:
    conv.ready[rank] = {
        "draft_id": draft_id, "content_hash": content_hash, "hook": hook, "gate_status": gate_status,
    }
    conv.pending_ranks.discard(rank)
    if not conv.pending_ranks:
        conv.state = State.PREVIEW_SENT
    how_to_publish = f"publish {rank}" if len(conv.digest_items) > 1 else "publish"
    text = (
        f"{report_card}\n\n--- draft ---\n{draft_text}\n\n"
        f"Reply '{how_to_publish}', or tell me what to change."
    )
    return Action("send_message", text=text)


def draft_needs_review(
    conv: Conversation, rank: int, draft_id: str, content_hash: str, reasons: list[str],
    report_card: str | None = None, draft_text: str | None = None,
) -> Action:
    """Still delivers the draft — a failed gate means "don't auto-publish
    this," not "show Scott nothing." Stored in `ready` alongside clean
    drafts (gate_status="needs_review") so "edit N" can find and re-draft
    it exactly like any other item; only the publish path discriminates on
    gate_status (see the approve_publish branch in `handle`). Publishing
    stays hard-blocked regardless — that check is enforced again in
    pipeline/jobs/draft_case.py::run_publish, not just here."""
    conv.ready[rank] = {
        "draft_id": draft_id, "content_hash": content_hash, "hook": f"case {rank}",
        "gate_status": "needs_review", "reasons": reasons,
    }
    conv.pending_ranks.discard(rank)
    if not conv.pending_ranks:
        conv.state = State.PREVIEW_SENT if has_actionable(conv) else State.NEEDS_REVIEW

    parts = [f"{rank}️⃣ didn't clear verification:\n" + "\n".join(f"- {r}" for r in reasons)]
    if report_card:
        parts.append(report_card)
    if draft_text:
        parts.append(f"--- draft text ---\n{draft_text}")
    parts.append(f"Not publishable as-is (reply 'edit {rank}: ...' to try a fix, or handle it manually).")
    return Action("send_message", text="\n\n".join(parts))


def _target_rank(conv: Conversation, slot: str | None) -> int | None:
    if slot:
        ranks = _parse_ranks(slot)
        if len(ranks) == 1 and ranks[0] in conv.ready:
            return ranks[0]
        return None
    if len(conv.ready) == 1:
        return next(iter(conv.ready))
    return None


def _ambiguous_rank_message(conv: Conversation, verb: str) -> str:
    if not conv.ready:
        return "Nothing's ready yet — I'll message you as soon as a draft clears."
    options = ", ".join(
        f"{r}{'' if _is_actionable(v) else ' (needs a fix first)'}"
        for r, v in sorted(conv.ready.items())
    )
    return f"Which one? Reply '{verb} N' — ready: {options}"
