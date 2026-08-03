"""V1 gate — coverage backtest (plan §2.5 / Phase 1 acceptance gate).

Samples N historical Scott posts, extracts the case they were about, and
checks whether that case is findable today via CourtListener (opinions
search + RECAP search, both unauthenticated in this run). Reports the %
coverage and which threshold tier applies:
  >=70%  -> ship ingestion as designed
  40-70% -> RECAP member alerts + weekly GovInfo reconciliation become mandatory
  <40%   -> promote the manual-forward path to primary; ask Scott about WestClip

Run: uv run python -m verify.v1_backtest_coverage [--n 20]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from pathlib import Path

from rapidfuzz import fuzz

from pipeline.ingest.courtlistener import CourtListenerClient

ROOT = Path(__file__).resolve().parent.parent
CORPUS_DB = ROOT / "corpus" / "corpus.sqlite"
OUT = ROOT / "verify" / "v1_results.json"

# Case name from the FIRST "In *Case v. Case*," or "In Case v. Case," mention
# near the start of the intro — this is Scott's near-universal opening shape.
CASE_NAME_RE = re.compile(
    r"^\s*In\s+\*?([A-Z][A-Za-z.,'&\-\s]{3,90}?\bv\.?\s[A-Za-z.,'&\-\s]{2,70}?)\*?,",
    re.I,
)


def extract_case_name(intro: str) -> str | None:
    m = CASE_NAME_RE.search(intro or "")
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip(" ,*")


def select_sample(n: int) -> list[dict]:
    conn = sqlite3.connect(CORPUS_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT date, url, intro, court_id, closing_citation FROM posts "
        "WHERE author = 'Scott J. Hyman' AND date IS NOT NULL "
        "ORDER BY date"
    ).fetchall()
    conn.close()

    candidates = []
    for r in rows:
        name = extract_case_name(r["intro"])
        if name and r["court_id"]:
            candidates.append({**dict(r), "case_name": name})

    if not candidates:
        return []
    step = max(1, len(candidates) // n)
    return candidates[::step][:n]


def check_case(client: CourtListenerClient, case: dict) -> dict:
    name, court = case["case_name"], case["court_id"]
    result = {**case, "found_opinions": False, "found_recap": False, "match_detail": None}

    try:
        hits = client.search_opinions(name, courts=[court] if court else None)
    except Exception as e:  # noqa: BLE001
        hits = []
        result["opinions_error"] = str(e)
    for h in hits[:5]:
        if fuzz.token_set_ratio(name, h.case_name) >= 80:
            result["found_opinions"] = True
            result["match_detail"] = {"source": "opinions", "matched_name": h.case_name, "cluster_id": h.cluster_id}
            break

    if not result["found_opinions"]:
        try:
            recap_hits = client.search_recap(name, courts=[court] if court else None)
        except Exception as e:  # noqa: BLE001
            recap_hits = []
            result["recap_error"] = str(e)
        for rh in recap_hits[:5]:
            rname = rh.get("caseName", "")
            if rname and fuzz.token_set_ratio(name, rname) >= 80:
                result["found_recap"] = True
                result["match_detail"] = {"source": "recap", "matched_name": rname, "docket_id": rh.get("docket_id")}
                break

    return result


def main(n: int) -> None:
    sample = select_sample(n)
    print(f"selected {len(sample)} historical cases with extractable name+court")
    client = CourtListenerClient()
    results = []
    for i, case in enumerate(sample):
        print(f"[{i+1}/{len(sample)}] {case['date'][:10]} {case['court_id']}: {case['case_name'][:70]}")
        r = check_case(client, case)
        status = "OPINIONS" if r["found_opinions"] else ("RECAP" if r["found_recap"] else "NOT FOUND")
        print(f"    -> {status}")
        results.append(r)
        time.sleep(1)  # stay well under the 5/min unauthenticated ceiling given 2 calls/case
    client.close()

    n_total = len(results)
    n_opinions = sum(1 for r in results if r["found_opinions"])
    n_recap = sum(1 for r in results if r["found_recap"])
    n_either = sum(1 for r in results if r["found_opinions"] or r["found_recap"])
    pct = round(100 * n_either / max(n_total, 1), 1)

    if pct >= 70:
        tier = "SHIP_AS_DESIGNED (>=70%)"
    elif pct >= 40:
        tier = "RECAP_ALERTS_MANDATORY (40-70%)"
    else:
        tier = "MANUAL_FORWARD_PRIMARY (<40%)"

    summary = {
        "n_total": n_total, "n_found_opinions": n_opinions, "n_found_recap": n_recap,
        "n_found_either": n_either, "coverage_pct": pct, "tier": tier,
        "note": "Run WITHOUT a CourtListener token/FLP membership — this is the "
                "conservative floor. Coverage with a token (esp. RECAP search "
                "alerts, member-only) will be higher; re-run once COURTLISTENER_TOKEN "
                "is set to get the number that actually governs the go-live decision.",
        "results": results,
    }
    OUT.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(f"\n=== COVERAGE: {n_either}/{n_total} = {pct}% ===")
    print(f"=== TIER: {tier} ===")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    args = ap.parse_args()
    main(args.n)
