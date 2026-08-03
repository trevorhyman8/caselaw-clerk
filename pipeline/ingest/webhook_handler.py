"""CourtListener search-alert webhook payload -> evidence locker. Payload
shape per CourtListener's webhook docs: {"webhook": {...}, "payload":
{"results": [...]}} where each result matches the same shape as a /search/
response hit. NOT LIVE-TESTED — requires a live CourtListener account with a
saved search alert configured to deliver via webhook (config/queries.yaml
defines the 5 alerts to create; SETUP.md Phase 1 has the exact steps). The
extraction logic below reuses the same OpinionHit-shaped parsing as
sweep.py so both paths land in the locker identically.
"""
from __future__ import annotations

import logging

from pipeline.ingest.courtlistener import fetch_opinion_text
from pipeline.ingest.locker import NewCase, add_artifact, upsert_case

log = logging.getLogger("webhook_handler")


def handle_courtlistener_alert(payload: dict) -> dict:
    results = payload.get("payload", {}).get("results", []) or payload.get("results", [])
    stats = {"received": len(results), "new_cases": 0, "artifacts_fetched": 0}

    for res in results:
        opinions = res.get("opinions") or [{}]
        new_case = NewCase(
            case_name=res.get("caseName", ""),
            court_id=res.get("court_id", ""),
            docket_number=res.get("docketNumber"),
            judge=res.get("judge") or None,
            date_filed=res.get("dateFiled"),
            cl_cluster_id=res.get("cluster_id"),
            cl_docket_id=res.get("docket_id"),
            source="courtlistener_alert",
            source_url="https://www.courtlistener.com" + res.get("absolute_url", ""),
            raw_payload=res,
        )
        case_id, was_new = upsert_case(new_case)
        if was_new:
            stats["new_cases"] += 1

        download_url = opinions[0].get("download_url") if opinions else None
        if download_url:
            try:
                pdf_bytes, text = fetch_opinion_text(download_url)
                add_artifact(case_id, "opinion_pdf", pdf_bytes, None, download_url)
                if text.strip():
                    add_artifact(case_id, "opinion_text", None, text, download_url)
                stats["artifacts_fetched"] += 1
            except Exception as e:  # noqa: BLE001
                log.warning(f"artifact fetch failed for {new_case.case_name!r}: {e}")

    return stats
