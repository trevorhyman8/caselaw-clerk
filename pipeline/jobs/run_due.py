"""Cron-runner entrypoint (Railway cron, every 30 min per railway.json).
Reads config/config.yaml for timing, runs whichever jobs are due this tick:
  - sweep: secondary-source ingestion (always, every tick — cheap, mostly
    free-tier API calls; CourtListener's own budget is enforced inside the
    client's TokenBucket, not here)
  - morning digest: once/day at digest.send_time_local
  - weekly recap: once/week at weekly_recap_day + weekly_recap_time_local

NOT LIVE-TESTED end-to-end on Railway (needs the deployed cron service +
Postgres + a live Telegram bot token — all backloaded operator steps). The
sweep and scoring logic ARE tested (pipeline/ingest/sweep.py,
pipeline/score/score.py) against real data during dev.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from pipeline.db.db import init_schema
from pipeline.jobs.morning_digest import maybe_send_digest
from pipeline.jobs.weekly_recon import maybe_run_weekly_recap
from pipeline.settings import load_config

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("run_due")


def run_sweep_tick() -> None:
    from pipeline.ingest.sweep import sweep

    cfg = load_config()["sources"]
    courts = []
    if cfg.get("federal_core"):
        courts += ["cacd", "cand", "casd", "caed"]
    if cfg.get("california_appellate"):
        courts += ["cal", "calctapp"]
    families = ["fdcpa", "tcpa", "fcra", "privacy", "arbitration", "class", "ucl"]
    try:
        result = sweep(courts=courts or ["cacd"], families=families, limit=10)
        log.info(f"sweep tick: {result}")
    except Exception as e:  # noqa: BLE001 — a failed tick should not crash the cron loop
        log.error(f"sweep tick failed: {e}")

    if cfg.get("california_appellate"):
        _sweep_calcourts()
    if cfg.get("cfpb_fcc"):
        _sweep_cfpb()


def _sweep_calcourts() -> None:
    from pipeline.ingest.calcourts import fetch, to_court_id
    from pipeline.ingest.courtlistener import fetch_opinion_text
    from pipeline.ingest.locker import NewCase, add_artifact, upsert_case

    for published in (True, False):
        try:
            for op in fetch(published=published, limit=20):
                case_id, was_new = upsert_case(
                    NewCase(
                        case_name=op.case_name, court_id=to_court_id(op),
                        docket_number=op.case_number, date_filed=op.date_filed,
                        source="calcourts", source_url=op.pdf_url or "",
                        raw_payload={"case_number": op.case_number, "published": op.published},
                    )
                )
                if was_new and op.pdf_url:
                    try:
                        pdf_bytes, text = fetch_opinion_text(op.pdf_url)
                        add_artifact(case_id, "opinion_pdf", pdf_bytes, None, op.pdf_url)
                        if text.strip():
                            add_artifact(case_id, "opinion_text", None, text, op.pdf_url)
                    except Exception as e:  # noqa: BLE001
                        log.warning(f"calcourts artifact fetch failed for {op.case_name!r}: {e}")
        except Exception as e:  # noqa: BLE001
            log.error(f"calcourts sweep ({'published' if published else 'unpublished'}) failed: {e}")


def _sweep_cfpb() -> None:
    from pipeline.ingest.agencies import fetch_cfpb

    try:
        items = fetch_cfpb(limit=10)
        log.info(f"cfpb sweep: {len(items)} items (agency items are not currently scored/drafted "
                 f"as cases — see pipeline/ingest/GAPS.md; logged for now, full pipeline TODO)")
    except Exception as e:  # noqa: BLE001
        log.error(f"cfpb sweep failed: {e}")


def main() -> None:
    init_schema()
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    log.info(f"run_due tick at {now.isoformat()}")

    run_sweep_tick()
    maybe_send_digest(now)
    maybe_run_weekly_recap(now)


if __name__ == "__main__":
    main()
