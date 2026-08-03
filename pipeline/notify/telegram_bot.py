"""Thin Telegram wiring — feeds inbound updates into the pure state machine
(state_machine.py) and executes whatever Action comes back. NOT LIVE-TESTED:
needs TELEGRAM_BOT_TOKEN, which is a [HUMAN: SCOTT-or-TREVOR] step (create a
bot via @BotFather, ~2 minutes, $0) bundled into SETUP.md. The state machine
and intent parser this wraps ARE tested (see the unit test run during dev).

Allowlist: only telegram_allowed_user_ids (config) get responses at all —
everyone else is silently logged, not replied to, so an unknown sender can't
even probe that the bot exists. Idempotency: python-telegram-bot's Updater
already dedupes by update_id; conversation state additionally checks
draft/content hashes before publish (see state_machine.py docstring) so a
double-delivered webhook can't double-post.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from pipeline.db import db
from pipeline.notify import state_machine as sm
from pipeline.notify.intent_parser import parse as parse_intent
from pipeline.settings import settings

log = logging.getLogger("telegram_bot")

# In-memory for the ACTIVE-conversation fields (edit rounds, confirm
# timers) — cheap, and fine to lose on a restart mid-edit. But digest_items
# is hydrated from the `digests` table (see _hydrate_from_latest_digest)
# rather than trusted from memory alone: the digest can be produced by a
# SEPARATE process (the cron job, or a manual trigger like
# jobs/send_digest_now.py) that has no way to reach into this process's
# memory. Found live: sending the digest via send_digest_now.py while this
# bot was already running left the in-memory conversation stuck at IDLE
# with zero digest_items, so a reply like "3" parsed as unknown intent
# instead of select_candidate. Rebuilding from the DB on every message
# fixes that AND makes a bot restart mid-cycle non-fatal, for free.
_conversations: dict[str, sm.Conversation] = {}


def _hydrate_from_latest_digest(conv: sm.Conversation) -> None:
    if conv.state != sm.State.IDLE:
        return
    with db.get_conn() as db_conn:
        db_conn.row_factory = sqlite3.Row
        row = db_conn.execute(
            db.qmark("SELECT items, created_at FROM digests ORDER BY created_at DESC LIMIT 1")
        ).fetchone()
    if not row:
        return
    from datetime import datetime, timedelta, timezone

    created_at = datetime.fromisoformat(row["created_at"])
    if datetime.now(timezone.utc) - created_at > timedelta(hours=20):
        return  # stale digest (yesterday or older) — don't resurrect it silently
    items = db.loads(row["items"])
    if items:
        conv.digest_items = items
        conv.state = sm.State.DIGEST_SENT


def _log_message(direction: str, user: str, text: str, parsed: dict | None = None) -> None:
    with db.get_conn() as conn:
        conn.execute(
            db.qmark(
                "INSERT INTO sms_log (id, direction, telegram_user, text, parsed_intent, created_at) "
                "VALUES (?,?,?,?,?,?)"
            ),
            (db.new_id(), direction, user, text, db.dumps(parsed), db.now_iso()),
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    text = update.message.text or ""

    if user_id not in settings.telegram_allowlist:
        log.warning(f"message from non-allowlisted user {user_id}: {text[:50]!r} — silently ignored")
        _log_message("in", user_id, text, {"intent": "REJECTED_NOT_ALLOWLISTED"})
        return

    _log_message("in", user_id, text)
    conv = _conversations.setdefault(user_id, sm.Conversation(telegram_user_id=user_id))
    _hydrate_from_latest_digest(conv)

    context_str = f"conversation_state={conv.state.value}, digest_items={len(conv.digest_items)}"
    parsed = parse_intent(text, context_str)
    _log_message("in", user_id, text, {"intent": parsed.intent, "slot": parsed.slot, "confidence": parsed.confidence})

    if not parsed.actionable and parsed.intent != "status":
        await update.message.reply_text(
            "Not sure what you mean — could you rephrase, or reply with a number from the digest?"
        )
        return

    action = sm.handle(conv, parsed.intent, parsed.slot, text)

    if action.kind == "send_message" and action.text:
        await update.message.reply_text(action.text)
        _log_message("out", user_id, action.text)
    elif action.kind == "start_draft":
        await update.message.reply_text(action.text or "Drafting...")
        _log_message("out", user_id, action.text or "")
        asyncio.ensure_future(_run_batch_and_reply(update, conv, action.case_ids or [], action.ranks or []))
    elif action.kind == "start_revision":
        await update.message.reply_text("On it.")
        rank = (action.ranks or [None])[0]
        asyncio.ensure_future(_run_revision_and_reply(update, conv, rank, action.draft_id, action.edit_instruction))
    elif action.kind == "do_publish":
        await _run_publish_and_reply(update, action.draft_id, action.content_hash)


async def _run_batch_and_reply(update: Update, conv: sm.Conversation, case_ids: list[str], ranks: list[int]) -> None:
    """Fans out N cases concurrently — each is a blocking, multi-LLM-call
    pipeline (pipeline/jobs/draft_case.py), so they run in a thread pool via
    run_in_executor rather than sequentially. Scott picks cases during a
    ~20-minute commute; the design constraint is "several minutes per case
    is fine, but picking 3 shouldn't take 3x as long as picking 1." Each
    report card is sent to Telegram the moment ITS draft resolves — nobody
    waits for the slowest one."""
    from pipeline.jobs.draft_case import run_draft_pipeline

    loop = asyncio.get_running_loop()

    async def _one(case_id: str, rank: int) -> None:
        hook = next((i["hook"] for i in conv.digest_items if i["rank"] == rank), f"case {rank}")
        try:
            result = await loop.run_in_executor(None, run_draft_pipeline, case_id)
        except Exception as e:  # noqa: BLE001 — one case failing must not sink the others
            log.exception(f"draft pipeline failed for rank {rank} ({case_id})")
            action = sm.draft_needs_review(conv, rank, [f"internal error: {e}"])
            await update.message.reply_text(action.text)
            return
        if result["gate_verdict"] in ("pass", "warn"):
            action = sm.draft_ready(
                conv, rank, result["draft_id"], result["content_hash"],
                result["report_card"], result["preview_url"], hook,
            )
        else:
            action = sm.draft_needs_review(conv, rank, result["reasons"])
        await update.message.reply_text(action.text)
        _log_message("out", conv.telegram_user_id, action.text)

    await asyncio.gather(*(_one(cid, r) for cid, r in zip(case_ids, ranks)))


async def _run_revision_and_reply(update: Update, conv: sm.Conversation, rank: int | None, draft_id: str | None, instruction: str) -> None:
    from pipeline.jobs.draft_case import run_revision_pipeline

    hook = next((i["hook"] for i in conv.digest_items if i["rank"] == rank), f"case {rank}")
    result = run_revision_pipeline(draft_id, instruction)
    if result["gate_verdict"] in ("pass", "warn"):
        action = sm.draft_ready(
            conv, rank, result["draft_id"], result["content_hash"],
            result["report_card"], result["preview_url"], hook,
        )
    else:
        action = sm.draft_needs_review(conv, rank, result["reasons"])
    await update.message.reply_text(action.text)


async def _run_publish_and_reply(update: Update, draft_id: str, content_hash: str | None) -> None:
    from pipeline.jobs.draft_case import run_publish

    result = run_publish(draft_id, expected_content_hash=content_hash)
    await update.message.reply_text(result["message"])


def build_app() -> Application:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set — see SETUP.md Phase 3")
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    build_app().run_polling()
