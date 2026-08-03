"""Builds and (if a Telegram token is configured) sends the morning digest.
Scores every 'captured' case not yet digested, applies the deterministic
floor/cap from config/scoring.yaml, and — per config.yaml's
auto_predraft_top_n — pre-drafts the top N via the SAME pipeline the
interactive "reply 1" path uses (pipeline/jobs/draft_case.py), so a tap on
an already-drafted digest item returns in seconds instead of ~90s.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from pipeline.db import db
from pipeline.score.score import build_digest, score_case
from pipeline.settings import load_config, settings

log = logging.getLogger("morning_digest")


def _due_now(now: datetime, send_time_local: str) -> bool:
    hh, mm = (int(x) for x in send_time_local.split(":"))
    return now.hour == hh and now.minute < 30  # cron runs every 30 min


def _already_sent_today(now: datetime) -> bool:
    with db.get_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            db.qmark("SELECT id FROM digests WHERE substr(created_at, 1, 10) = ? LIMIT 1")
        , (now.date().isoformat(),)).fetchone()
        return row is not None


def score_pending_cases() -> list[dict]:
    with db.get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            db.qmark(
                "SELECT c.id as case_id, c.case_name, c.court_id, c.date_filed, c.judge, "
                "a.content_text FROM cases c JOIN artifacts a ON a.case_id = c.id "
                "WHERE c.status = 'captured' AND a.kind = 'opinion_text'"
            )
        ).fetchall()

    scored = []
    for r in rows:
        result = score_case(r["content_text"], r["court_id"], judge=r["judge"])
        if result["score"] > 0:
            scored.append({
                "case_id": r["case_id"], "case_name": r["case_name"],
                "court_id": r["court_id"], "date_filed": r["date_filed"],
                "score": result["score"], "breakdown": result["breakdown"],
            })
    return scored


def _hook_for(case: dict) -> str:
    stat = case["breakdown"].get("primary_statute", "").upper()
    return f"{case['court_id'].upper()}: {stat} — {case['case_name'][:60]}"


def maybe_send_digest(now: datetime) -> None:
    cfg = load_config()["digest"]
    if not _due_now(now, cfg["send_time_local"]) or _already_sent_today(now):
        return

    scored = score_pending_cases()
    digest_items = build_digest(scored)

    if not digest_items and cfg.get("silent_on_no_cases", True):
        log.info("no digest-worthy cases today, staying silent per config")
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

    predraft_n = load_config()["drafting"]["auto_predraft_top_n"]
    if predraft_n:
        _predraft_top(items_for_storage[:predraft_n])

    _deliver(items_for_storage)


def _predraft_top(items: list[dict]) -> None:
    from pipeline.jobs.draft_case import run_draft_pipeline

    for item in items:
        try:
            result = run_draft_pipeline(item["case_id"])
            log.info(f"pre-drafted {item['case_id']}: verdict={result['gate_verdict']}")
        except Exception as e:  # noqa: BLE001 — a pre-draft failure just means
            log.warning(f"pre-draft failed for {item['case_id']}: {e}")  # slower interactive draft later


def _deliver(items: list[dict]) -> None:
    if not settings.telegram_bot_token:
        log.info(f"[no TELEGRAM_BOT_TOKEN configured — digest built but not sent] {items}")
        return
    # Actual send wires through python-telegram-bot's Bot.send_message to
    # every id in settings.telegram_allowlist — deferred to when a live bot
    # token exists (SETUP.md Phase 3); the digest-building logic above is
    # already fully testable without one.
    log.info(f"would send digest to {settings.telegram_allowlist}: {items}")
