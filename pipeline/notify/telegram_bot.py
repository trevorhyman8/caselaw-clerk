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

import logging

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from pipeline.db import db
from pipeline.notify import state_machine as sm
from pipeline.notify.intent_parser import parse as parse_intent
from pipeline.settings import settings

log = logging.getLogger("telegram_bot")

# In-memory for now — conversations are short-lived (one digest cycle) and a
# restart mid-cycle just means the next digest re-establishes state. If this
# ever needs to survive restarts mid-conversation, persist Conversation to
# Postgres (schema.sql has no table for it yet — deliberately deferred until
# proven necessary rather than speculatively built).
_conversations: dict[str, sm.Conversation] = {}


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
        await _run_draft_and_reply(update, conv, action.case_id)
    elif action.kind == "start_revision":
        await update.message.reply_text("On it.")
        await _run_revision_and_reply(update, conv, action.draft_id, action.edit_instruction)
    elif action.kind == "do_publish":
        await _run_publish_and_reply(update, conv, action.draft_id)


async def _run_draft_and_reply(update: Update, conv: sm.Conversation, case_id: str) -> None:
    """Wires the case_id -> fact_sheet -> draft -> gate pipeline (see
    pipeline/jobs/draft_case.py for the actual implementation shared with
    the overnight Batch pre-drafting path) and sends the resulting preview
    or NEEDS_REVIEW message via the state machine's draft_ready /
    draft_needs_review helpers."""
    from pipeline.jobs.draft_case import run_draft_pipeline

    result = run_draft_pipeline(case_id)
    if result["gate_verdict"] in ("pass", "warn"):
        action = sm.draft_ready(
            conv, result["draft_id"], result["content_hash"],
            result["report_card"], result["preview_url"],
        )
    else:
        action = sm.draft_needs_review(conv, result["reasons"])
    await update.message.reply_text(action.text)


async def _run_revision_and_reply(update: Update, conv: sm.Conversation, draft_id: str, instruction: str) -> None:
    from pipeline.jobs.draft_case import run_revision_pipeline

    result = run_revision_pipeline(draft_id, instruction)
    if result["gate_verdict"] in ("pass", "warn"):
        action = sm.draft_ready(
            conv, result["draft_id"], result["content_hash"],
            result["report_card"], result["preview_url"],
        )
    else:
        action = sm.draft_needs_review(conv, result["reasons"])
    await update.message.reply_text(action.text)


async def _run_publish_and_reply(update: Update, conv: sm.Conversation, draft_id: str) -> None:
    from pipeline.jobs.draft_case import run_publish

    result = run_publish(draft_id, expected_content_hash=conv.active_draft_content_hash)
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
