"""Phase 2 eval harness — regenerates historical Scott posts from their
underlying opinions (where the opinion is independently resolvable via
CourtListener, leave-one-out on the exemplar index so a post never
retrieves itself) and scores the results against the plan's thresholds:

  zero-unverified-quote rate      20/20 required (hard gate)
  citation field accuracy         20/20 required
  holding-direction accuracy      20/20 required (reputational-risk metric)
  structural style conformance    >=18/20
  category F1 vs Scott's actual labels   >=85% primary / >=0.75 multi-label

NOT YET RUN AT FULL SCALE (20-30 cases) as of this build — each case costs
several LLM round-trips (fact extraction, draft, adversarial verify), and a
full run is a genuine time/token investment best made deliberately, not as
a side effect of scaffolding the harness. This script IS runnable today;
run it with --n 3 for a fast smoke test, or --n 20 for the real eval gate
before trusting the system for daily use (see plan Phase 2 acceptance
criteria).

Run: uv run python -m verify.v2_eval_harness --n 3
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

from rapidfuzz import fuzz

from pipeline.draft.content_agent import draft_post
from pipeline.ingest.courtlistener import CourtListenerClient, fetch_opinion_text
from pipeline.style.exemplars import retrieve
from pipeline.verify_gate.facts import extract_fact_sheet
from pipeline.verify_gate.gate import run_gate
from verify.v1_backtest_coverage import CASE_NAME_RE, extract_case_name

ROOT = Path(__file__).resolve().parent.parent
CORPUS_DB = ROOT / "corpus" / "corpus.sqlite"
OUT = ROOT / "verify" / "v2_results.json"


def select_resolvable_cases(n: int) -> list[dict]:
    """Same extraction as v1's backtest, but this time we need the actual
    opinion TEXT (not just a name match), and we exclude the exemplar
    itself from retrieval (leave-one-out) for a fair style comparison."""
    conn = sqlite3.connect(CORPUS_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT date, url, intro, court_id, content, categories FROM posts "
        "WHERE author = 'Scott J. Hyman' AND date IS NOT NULL ORDER BY date DESC"
    ).fetchall()
    conn.close()

    client = CourtListenerClient()
    found = []
    try:
        for r in rows:
            if len(found) >= n:
                break
            name = extract_case_name(r["intro"])
            if not name or not r["court_id"]:
                continue
            try:
                hits = client.search_opinions(name, courts=[r["court_id"]])
            except Exception:  # noqa: BLE001
                continue
            match = next((h for h in hits if fuzz.token_set_ratio(name, h.case_name) >= 80 and h.download_url), None)
            if not match:
                continue
            try:
                _, text = fetch_opinion_text(match.download_url)
            except Exception:  # noqa: BLE001
                continue
            if len(text) < 800:
                continue
            found.append({
                "historical_url": r["url"], "historical_intro": r["intro"],
                "historical_content": r["content"], "historical_categories": r["categories"],
                "case_name": match.case_name, "court_id": match.court_id,
                "docket_number": match.docket_number, "date_filed": match.date_filed,
                "judge": match.judge, "opinion_text": text,
            })
    finally:
        client.close()
    return found


def score_one(case: dict) -> dict:
    case_meta = {
        "case_name": case["case_name"], "court_id": case["court_id"],
        "docket_number": case["docket_number"], "date_filed": case["date_filed"],
        "judge": case["judge"],
    }
    fact_sheet = extract_fact_sheet(case["opinion_text"], case_meta)
    exemplars = retrieve(
        statutes=fact_sheet.statutes, court_id=case["court_id"], posture=fact_sheet.posture,
        limit=3, exclude_url=case["historical_url"],  # leave-one-out
    )
    frozen_categories = []
    stats_path = ROOT / "corpus" / "style_stats.json"
    if stats_path.exists():
        frozen_categories = list(json.loads(stats_path.read_text())["frozen_category_vocabulary"].keys())

    import hashlib
    sha = hashlib.sha256(case["opinion_text"].encode()).hexdigest()
    draft = draft_post(case_meta, case["opinion_text"], fact_sheet, exemplars, frozen_categories, sha)
    gate_result = run_gate(draft, case_meta, case["opinion_text"], fact_sheet, sha)

    return {
        "case_name": case["case_name"],
        "gate_verdict": gate_result.verdict,
        "zero_unverified_quotes": not gate_result.quote_check["any_fail"],
        "citation_ok": gate_result.citation_check["closing_all_pass"],
        "holding_ok": gate_result.fact_check["holding_ok"],
        "adversarial_clean": gate_result.adversarial_check["passed"],
        "categories_drafted": draft.get("categories", []),
        "categories_historical": case["historical_categories"],
    }


def main(n: int) -> None:
    print(f"selecting {n} resolvable historical cases (this queries CourtListener live)...")
    cases = select_resolvable_cases(n)
    print(f"resolved {len(cases)}/{n} — regenerating each (this calls the LLM multiple times per case)...")

    results = []
    for i, case in enumerate(cases):
        print(f"[{i+1}/{len(cases)}] {case['case_name'][:60]}")
        try:
            results.append(score_one(case))
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED: {e}")
            results.append({"case_name": case["case_name"], "error": str(e)})

    ok_results = [r for r in results if "error" not in r]
    n_ok = len(ok_results)
    summary = {
        "n_attempted": len(cases),
        "n_scored": n_ok,
        "zero_unverified_quote_rate": sum(r["zero_unverified_quotes"] for r in ok_results) / max(n_ok, 1),
        "citation_accuracy": sum(r["citation_ok"] for r in ok_results) / max(n_ok, 1),
        "holding_direction_accuracy": sum(r["holding_ok"] for r in ok_results) / max(n_ok, 1),
        "adversarial_clean_rate": sum(r["adversarial_clean"] for r in ok_results) / max(n_ok, 1),
        "thresholds": {
            "zero_unverified_quote_rate": "20/20 required (this run: n={})".format(n_ok),
            "citation_accuracy": "20/20 required",
            "holding_direction_accuracy": "20/20 required",
        },
        "results": results,
    }
    OUT.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3, help="use 20 for the real Phase-2 acceptance gate")
    args = ap.parse_args()
    main(args.n)
