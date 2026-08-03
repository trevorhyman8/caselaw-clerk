"""Weekly recap (skipped cases the digest surfaced but Scott didn't act on)
+ GovInfo reconciliation stub. Recap logic is fully testable without
secrets; GovInfo reconciliation needs GOVINFO_API_KEY (free, self-serve —
SETUP.md) and is stubbed pending that.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

from pipeline.db import db
from pipeline.settings import load_config, settings

log = logging.getLogger("weekly_recon")

DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _due_now(now: datetime, cfg: dict) -> bool:
    if not cfg.get("weekly_recap"):
        return False
    target_day = cfg["weekly_recap_day"].lower()
    hh, mm = (int(x) for x in cfg["weekly_recap_time_local"].split(":"))
    return DAY_NAMES[now.weekday()] == target_day and now.hour == hh and now.minute < 30


def skipped_cases(expiry_days: int) -> list[dict]:
    cutoff = (datetime.now() - timedelta(days=expiry_days)).isoformat()
    with db.get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            db.qmark(
                "SELECT id as case_id, case_name, court_id FROM cases "
                "WHERE status IN ('digested', 'captured') AND updated_at > ?"
            ),
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]


def maybe_run_weekly_recap(now: datetime) -> None:
    cfg = load_config()["digest"]
    if not _due_now(now, cfg):
        return

    skipped = skipped_cases(cfg["recap_expiry_days"])
    if not skipped:
        log.info("weekly recap: nothing skipped this week")
        return

    lines = [f"This week you had {len(skipped)} decision(s) not yet drafted:"]
    for i, c in enumerate(skipped, 1):
        lines.append(f"{i}️⃣ {c['court_id'].upper()}: {c['case_name']}")
    lines.append("Reply a number to draft any, or 'archive' to clear.")
    text = "\n".join(lines)

    if not settings.telegram_bot_token:
        log.info(f"[no TELEGRAM_BOT_TOKEN — recap built but not sent]\n{text}")
        return
    log.info(f"would send weekly recap: {text}")


def run_govinfo_reconciliation() -> dict:
    """Compares the locker's captured district-court cases against GovInfo's
    USCOURTS collection for the same week, surfacing anything GovInfo has
    that the locker doesn't (the coverage-gap signal from plan §2.5).
    STUBBED — needs GOVINFO_API_KEY (SETUP.md, free self-serve at
    api.data.gov). Returns a clearly-marked not-yet-implemented result
    rather than silently reporting zero misses."""
    if not settings.govinfo_api_key:
        return {"status": "not_configured", "note": "GOVINFO_API_KEY not set — see SETUP.md"}
    return {"status": "not_implemented", "note": "GovInfo API integration is designed but not yet built"}
