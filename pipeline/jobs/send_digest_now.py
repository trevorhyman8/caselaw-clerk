"""Manual trigger for local/demo testing — runs a sweep, scores whatever's
in the locker, and sends the digest immediately over Telegram, bypassing
morning_digest.py's time-of-day gate (which only fires at the configured
send_time_local). Not part of the production cron path; this exists purely
so a live end-to-end test doesn't require waiting for 5:45am.

Run: uv run python -m pipeline.jobs.send_digest_now
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3

from telegram import Bot

from pipeline.db import db
from pipeline.db.db import init_schema
from pipeline.jobs.morning_digest import _hook_for, score_pending_cases
from pipeline.score.score import build_digest
from pipeline.settings import settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("send_digest_now")


async def send(text: str) -> None:
    bot = Bot(token=settings.telegram_bot_token)
    for user_id in settings.telegram_allowlist:
        await bot.send_message(chat_id=int(user_id), text=text)
        log.info(f"sent to {user_id}")


def main() -> None:
    init_schema()

    if not settings.telegram_bot_token or not settings.telegram_allowlist:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS must be set in .env first."
        )

    scored = score_pending_cases()
    digest_items = build_digest(scored)

    if not digest_items:
        log.info("No digest-worthy cases currently in the locker. Run a sweep first:")
        log.info("  uv run python -m pipeline.ingest.sweep --courts ca9 cacd cand --families tcpa fdcpa fcra --limit 8")
        return

    items_for_storage = [
        {"case_id": c["case_id"], "rank": i + 1, "hook": _hook_for(c), "score": c["score"]}
        for i, c in enumerate(digest_items)
    ]
    with db.get_conn() as conn:
        conn.execute(
            db.qmark("INSERT INTO digests (id, sent_at, items, created_at) VALUES (?,?,?,?)"),
            (db.new_id(), db.now_iso(), db.dumps(items_for_storage), db.now_iso()),
        )
        for item in items_for_storage:
            conn.execute(
                db.qmark("UPDATE cases SET status = 'digested', updated_at = ? WHERE id = ?"),
                (db.now_iso(), item["case_id"]),
            )

    lines = [f"Good morning. {len(digest_items)} new decision(s) worth a look:"]
    for item in items_for_storage:
        lines.append(f"{item['rank']}️⃣ {item['hook']}")
    lines.append("Reply a number to draft, 'all' for all, or 'skip'.")
    text = "\n".join(lines)

    log.info(f"digest built:\n{text}")
    asyncio.run(send(text))


if __name__ == "__main__":
    main()
