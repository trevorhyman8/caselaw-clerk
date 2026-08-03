"""Gate orchestrator — runs layers 1-4 in order, produces the provenance
record and the human-visible report card. Fails closed: the publish path
checks `gate == "pass"` on the STORED provenance, never re-derives trust
from the model at publish time.

Verdicts:
  pass          all layers clean
  warn          only fuzzy-tier quote matches or non-blocking issues; a
                human should glance at it, but it's eligible for the normal
                approve flow
  needs_review  a hard failure survived one auto-repair attempt (or
                holding_direction mismatch, which never auto-repairs) —
                routed to a human, never auto-publishable
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from pipeline.ingest.courtlistener import CourtListenerClient
from pipeline.verify_gate.adversarial import verify as adversarial_verify
from pipeline.verify_gate.citations import check_closing_citation, check_other_citations
from pipeline.verify_gate.facts import FactSheet, assert_fact_consistency
from pipeline.verify_gate.quotes import check_quote_blocks


@dataclass
class GateResult:
    verdict: str  # pass | warn | needs_review
    quote_check: dict
    citation_check: dict
    fact_check: dict
    adversarial_check: dict
    quote_share: float | None
    reasons: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)


def run_gate(
    draft: dict,
    case_meta: dict,
    canonical_opinion_text: str,
    fact_sheet: FactSheet,
    artifact_sha256: str,
    cl_client: CourtListenerClient | None = None,
) -> GateResult:
    reasons: list[str] = []

    # L1 — quote integrity
    quote_blocks = [b["text"] for b in draft.get("blocks", []) if b.get("type") == "quote"]
    draft_body_chars = len(draft.get("intro", "") or "") + sum(
        len(b.get("text", "")) for b in draft.get("blocks", [])
    )
    qc = check_quote_blocks(quote_blocks, canonical_opinion_text, draft_body_chars=draft_body_chars)
    if qc.any_fail:
        reasons.append("L1: one or more quotes failed verbatim verification")
    fuzzy_count = sum(1 for s in qc.segments if s.tier == "fuzzy")
    if fuzzy_count:
        reasons.append(f"L1: {fuzzy_count} quote segment(s) only matched at fuzzy tier")
    if qc.quote_share is not None and qc.quote_share < 0.5:
        reasons.append(f"L1: quote_share {qc.quote_share:.2f} is below the 0.5 warn floor")
    if not qc.in_order:
        reasons.append("L1: quotes are not in document order")

    # L2 — citation integrity
    closing_result = check_closing_citation(draft.get("closing_citation", ""), case_meta)
    if not closing_result.all_pass:
        failed = [c.field for c in closing_result.checks if not c.passed]
        reasons.append(f"L2: closing citation field(s) failed: {failed}")

    other_citations = check_other_citations(draft.get("intro", ""), canonical_opinion_text, cl_client)
    unverified = [c for c in other_citations if not c.passed]
    if unverified:
        reasons.append(f"L2: {len(unverified)} other citation(s) unverified: {[c.raw for c in unverified]}")

    # L3 — fact scoping (fact sheet must already be self-verified at ingest;
    # this only checks the DRAFT's intro against it)
    fact_assertions = assert_fact_consistency(draft.get("intro", ""), fact_sheet, case_meta)
    holding_assertion = next((a for a in fact_assertions if a.name == "holding_direction"), None)
    holding_hard_fail = holding_assertion is not None and not holding_assertion.passed
    other_fact_fails = [a for a in fact_assertions if a.name != "holding_direction" and not a.passed]
    if holding_hard_fail:
        reasons.append("L3: HOLDING DIRECTION MISMATCH — hard fail, requires full redraft, not a patch")
    if other_fact_fails:
        reasons.append(f"L3: fact assertion(s) failed: {[a.name for a in other_fact_fails]}")

    # L4 — adversarial verifier
    adv = adversarial_verify(canonical_opinion_text, draft)
    if not adv.passed:
        reasons.append(f"L4: adversarial verifier found {len(adv.violations)} unsupported claim(s)")

    # verdict
    hard_fail = qc.any_fail or not closing_result.all_pass or bool(unverified) or holding_hard_fail or not adv.passed
    if hard_fail:
        verdict = "needs_review"
    elif fuzzy_count or other_fact_fails or (qc.quote_share is not None and qc.quote_share < 0.5):
        verdict = "warn"
    else:
        verdict = "pass"

    provenance = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact_sha256": artifact_sha256,
        "quote_map": [
            {"text": s.text[:200], "tier": s.tier, "start": s.start, "end": s.end}
            for s in qc.segments
        ],
        "quote_share": qc.quote_share,
        "citations": {
            "closing": [asdict(c) for c in closing_result.checks],
            "other": [{"raw": c.raw, "method": c.method, "passed": c.passed} for c in other_citations],
        },
        "fact_assertions": [{"name": a.name, "passed": a.passed} for a in fact_assertions],
        "adversarial": {
            "prompt_sha256": adv.prompt_sha256,
            "violations": [{"draft_text": v.draft_text, "why": v.why} for v in adv.violations],
        },
        "verdict": verdict,
        "reasons": reasons,
    }
    provenance["provenance_sha256"] = hashlib.sha256(
        json.dumps(provenance, sort_keys=True, default=str).encode()
    ).hexdigest()

    return GateResult(
        verdict=verdict,
        quote_check={"all_exact": qc.all_exact, "any_fail": qc.any_fail, "fuzzy_count": fuzzy_count,
                     "quote_share": qc.quote_share, "in_order": qc.in_order},
        citation_check={"closing_all_pass": closing_result.all_pass, "other_unverified": len(unverified)},
        fact_check={"holding_ok": not holding_hard_fail, "other_fails": [a.name for a in other_fact_fails]},
        adversarial_check={"passed": adv.passed, "n_violations": len(adv.violations)},
        quote_share=qc.quote_share,
        reasons=reasons,
        provenance=provenance,
    )


def render_report_card(result: GateResult, case_name: str, court_label: str, source_url: str) -> str:
    icon = {"pass": "✅ VERIFIED", "warn": "⚠️ VERIFIED (warnings)", "needs_review": "❌ NEEDS REVIEW"}[result.verdict]
    lines = [f"{icon} — {case_name} ({court_label})"]

    q = result.quote_check
    n_exact = sum(1 for s in result.provenance["quote_map"] if s["tier"] == "exact")
    n_total_q = len(result.provenance["quote_map"])
    qs = f"{result.quote_share:.0%}" if result.quote_share is not None else "n/a"
    lines.append(f"├─ Quotes: {n_exact}/{n_total_q} passages verified verbatim ({qs} of body is the court's own words)")

    c = result.citation_check
    lines.append(f"├─ Citation: {'✓ all fields matched' if c['closing_all_pass'] else '✗ field mismatch — see detail'}")
    if c["other_unverified"]:
        lines.append(f"│   ⚠ {c['other_unverified']} other citation(s) unverified")

    f = result.fact_check
    lines.append(f"├─ Facts: holding direction {'✓' if f['holding_ok'] else '✗ MISMATCH'}"
                 + (f", other: {f['other_fails']}" if f["other_fails"] else ""))

    a = result.adversarial_check
    lines.append(f"└─ Independent AI audit: {a['n_violations']} unsupported claim(s) found")
    lines.append(f"\nSource: {source_url}")
    lines.append(f"Evidence sha256: {result.provenance['artifact_sha256'][:16]}…")
    return "\n".join(lines)
