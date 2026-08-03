"""V6 gate — calibration test: does the verification gate actually catch
deliberately corrupted drafts? Runs a small battery of known-bad drafts
against a real fetched opinion and confirms every one is caught. This is
the demo Trevor's plan calls for showing Scott before he trusts the system.

Run: uv run python verify/v6_gate_calibration.py
"""
from __future__ import annotations

import hashlib
import sys

from pipeline.ingest.courtlistener import CourtListenerClient, fetch_opinion_text
from pipeline.verify_gate.facts import extract_fact_sheet
from pipeline.verify_gate.gate import run_gate

# A real, small, fast-to-fetch opinion for calibration — chosen for size,
# not case selection bias (any real fetched opinion works for this test).
TEST_CASE_QUERY = "TCPA"
TEST_COURT = ["ca9"]


def _fetch_one() -> tuple[dict, str, str]:
    client = CourtListenerClient()
    try:
        hits = client.search_opinions(TEST_CASE_QUERY, courts=TEST_COURT)
        hit = next(h for h in hits if h.download_url)
        _, text = fetch_opinion_text(hit.download_url)
        case_meta = {
            "case_name": hit.case_name, "court_id": hit.court_id,
            "docket_number": hit.docket_number, "date_filed": hit.date_filed,
            "judge": hit.judge,
        }
        return case_meta, text, hashlib.sha256(text.encode()).hexdigest()
    finally:
        client.close()


def main() -> bool:
    print(f"fetching a real {TEST_CASE_QUERY} opinion for calibration...")
    case_meta, opinion, sha = _fetch_one()
    fact_sheet = extract_fact_sheet(opinion, case_meta)
    real_quote = opinion.strip().split("\n")[0][:150] or opinion[:150]

    corrupted_drafts = {
        "flipped_holding": {
            "intro": f"In {case_meta['case_name']}, the court affirmed everything, awarding no relief.",
            "blocks": [{"type": "quote", "text": "This sentence does not appear anywhere in the opinion at all."}],
            "closing_citation": f"{case_meta['case_name']}.",
        },
        "fabricated_damages": {
            "intro": f"In {case_meta['case_name']}, the court awarded $9.4 million in damages.",
            "blocks": [{"type": "quote", "text": "This sentence does not appear anywhere in the opinion at all."}],
            "closing_citation": f"{case_meta['case_name']}.",
        },
        "wrong_case_citation": {
            "intro": f"In {case_meta['case_name']}, the court ruled on an unrelated matter.",
            "blocks": [{"type": "quote", "text": "This sentence does not appear anywhere in the opinion at all."}],
            "closing_citation": "Totally Different Case v. Someone Else, No. 99-99999.",
        },
    }

    all_caught = True
    for name, draft in corrupted_drafts.items():
        result = run_gate(draft, case_meta, opinion, fact_sheet, sha)
        caught = result.verdict == "needs_review"
        print(f"{'CAUGHT' if caught else 'MISSED'}: {name} -> verdict={result.verdict}")
        all_caught = all_caught and caught

    print("PASS — gate caught every corrupted draft" if all_caught else "FAIL — a corrupted draft slipped through")
    return all_caught


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
