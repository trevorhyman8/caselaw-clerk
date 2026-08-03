"""Daily/on-demand sweep: run the configured keyword families against
CourtListener opinions (+ RECAP if a token is present), fetch each hit's
official PDF, and write into the evidence locker. This is the "secondary
backstop" path (§2.2 of the plan) — the webhook-alert path is a separate,
not-yet-wired component that needs a live CourtListener account (backloaded
operator step; alerts can't be created without one). Sweep works fully
unauthenticated today, just budget-constrained.

Run: uv run python -m pipeline.ingest.sweep [--courts cacd cand] [--families tcpa fdcpa] [--limit 5]
"""
from __future__ import annotations

import argparse
import logging

import yaml

from pipeline.ingest.courtlistener import CourtListenerClient, fetch_opinion_text
from pipeline.ingest.locker import NewCase, add_artifact, upsert_case
from pipeline.settings import ROOT

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("sweep")


def load_queries() -> dict:
    with open(ROOT / "config" / "queries.yaml") as f:
        return yaml.safe_load(f)


def build_query(families: list[str], q: dict) -> str:
    terms = []
    for fam in families:
        terms.extend(q["families"][fam]["terms"])
    return " OR ".join(terms)


def sweep(courts: list[str], families: list[str], limit: int, client: CourtListenerClient | None = None) -> dict:
    q = load_queries()
    query = build_query(families, q)
    cl = client or CourtListenerClient()
    stats = {"hits": 0, "new_cases": 0, "artifacts_fetched": 0, "artifacts_failed": 0}
    try:
        hits = cl.search_opinions(query, courts=courts)[:limit]
        stats["hits"] = len(hits)
        for h in hits:
            case_id, was_new = upsert_case(
                NewCase(
                    case_name=h.case_name,
                    court_id=h.court_id,
                    docket_number=h.docket_number,
                    judge=h.judge,
                    date_filed=h.date_filed,
                    cl_cluster_id=h.cluster_id,
                    cl_docket_id=h.docket_id,
                    source="courtlistener_sweep",
                    source_url=h.absolute_url,
                    raw_payload={
                        "case_name": h.case_name, "court_id": h.court_id,
                        "docket_number": h.docket_number, "date_filed": h.date_filed,
                    },
                )
            )
            if was_new:
                stats["new_cases"] += 1
            log.info(f"{'NEW ' if was_new else 'seen'} {h.court_id} {h.case_name!r} ({h.date_filed})")

            if h.download_url:
                try:
                    pdf_bytes, text = fetch_opinion_text(h.download_url)
                    add_artifact(case_id, "opinion_pdf", pdf_bytes, None, h.download_url)
                    if text.strip():
                        add_artifact(case_id, "opinion_text", None, text, h.download_url)
                    stats["artifacts_fetched"] += 1
                except Exception as e:  # noqa: BLE001 — log and continue the sweep
                    stats["artifacts_failed"] += 1
                    log.warning(f"  artifact fetch failed for {h.case_name!r}: {e}")
    finally:
        if client is None:
            cl.close()
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--courts", nargs="+", default=["ca9"])
    ap.add_argument("--families", nargs="+", default=["tcpa"])
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    from pipeline.db.db import init_schema

    init_schema()
    result = sweep(args.courts, args.families, args.limit)
    log.info(f"\nsweep result: {result}")
