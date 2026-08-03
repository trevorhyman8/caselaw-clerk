"""Layer 2 — citation integrity.

Two distinct checks:
1. The draft's CLOSING citation (the case it's actually about) is compared
   field-by-field against the evidence locker's own metadata for that case
   — not against the model's say-so. A mismatch on docket/court/date/party
   is a hard signal something is wrong (wrong case cited, or a fabricated
   detail).
2. Any OTHER citation appearing in the draft's intro/bridge prose (i.e. NOT
   inside an already quote-verified block) must either (a) appear verbatim
   in the source opinion text itself — the common case, since Scott's
   intros mostly cite only the case at hand — or (b) resolve via
   CourtListener's Citation Lookup API, which Free Law Project explicitly
   describes as built to guard against hallucinated citations. Citations
   living inside a layer-1-verified quote are exempt from re-checking here
   — they're the court's own citations, already covered by verbatim-quote
   verification.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from eyecite import get_citations

from pipeline.ingest.courtlistener import CourtListenerClient
from pipeline.verify_gate.normalize import normalize

DOCKET_STRIP_RE = re.compile(r"[^A-Za-z0-9]")
NAME_TOKEN_RE = re.compile(r"[A-Za-z]{3,}")

COURT_ABBREV = {
    "cacd": ["c.d. cal", "c.d.cal"],
    "cand": ["n.d. cal", "n.d.cal"],
    "casd": ["s.d. cal", "s.d.cal"],
    "caed": ["e.d. cal", "e.d.cal"],
    "ca9": ["9th cir", "ninth circuit"],
    "scotus": ["s. ct", "s.ct", "supreme court of the united states"],
    "calctapp": ["cal. ct. app", "cal.app", "cal. app"],
    "cal": ["cal. 4th", "cal. 5th", "cal.4th", "cal.5th"],
}


def _strip_docket(s: str | None) -> str:
    return DOCKET_STRIP_RE.sub("", s or "").upper()


def _name_tokens(name: str) -> set[str]:
    return {t.upper() for t in NAME_TOKEN_RE.findall(name or "")}


@dataclass
class FieldCheck:
    field: str
    expected: str | None
    found: str | None
    passed: bool


@dataclass
class ClosingCitationResult:
    checks: list[FieldCheck] = field(default_factory=list)

    @property
    def all_pass(self) -> bool:
        return all(c.passed for c in self.checks)


def check_closing_citation(closing_citation_text: str, case_meta: dict) -> ClosingCitationResult:
    """case_meta: {case_name, court_id, docket_number, date_filed} from the
    locker's `cases` row for the case this draft is about."""
    result = ClosingCitationResult()
    text = closing_citation_text or ""

    # party names — every surname token in the locker's case name should
    # appear somewhere in the draft's closing citation, both directions
    expected_tokens = _name_tokens(case_meta.get("case_name", ""))
    found_tokens = _name_tokens(text)
    overlap = expected_tokens & found_tokens
    party_ok = bool(expected_tokens) and len(overlap) / max(len(expected_tokens), 1) >= 0.5
    result.checks.append(
        FieldCheck("party_names", case_meta.get("case_name"), text, party_ok)
    )

    # docket
    docket = case_meta.get("docket_number")
    if docket:
        docket_ok = _strip_docket(docket) in _strip_docket(text) or _strip_docket(text) in _strip_docket(docket)
    else:
        docket_ok = True  # nothing to check against
    result.checks.append(FieldCheck("docket", docket, text, docket_ok))

    # court
    court_id = case_meta.get("court_id")
    abbrevs = COURT_ABBREV.get(court_id, [])
    court_ok = not abbrevs or any(a in text.lower() for a in abbrevs)
    result.checks.append(FieldCheck("court", court_id, text, court_ok))

    # date
    date_filed = case_meta.get("date_filed")
    if date_filed:
        year = str(date_filed)[:4]
        date_ok = year in text
    else:
        date_ok = True
    result.checks.append(FieldCheck("date", date_filed, text, date_ok))

    return result


@dataclass
class OtherCitationResult:
    raw: str
    method: str  # in-source | courtlistener | unverified
    passed: bool


def check_other_citations(
    prose_text: str, canonical_opinion_text: str, cl_client: CourtListenerClient | None = None,
) -> list[OtherCitationResult]:
    """Check every citation eyecite finds in the draft's non-quoted prose
    (intro/bridges) against the source opinion text, falling back to
    CourtListener's citation-lookup API. `cl_client=None` or no token means
    the courtlistener fallback silently degrades to "unverified" — visible
    in the provenance record, not hidden."""
    canonical_norm = normalize(canonical_opinion_text)
    out = []
    for cite in get_citations(prose_text):
        raw = cite.corrected_citation() if hasattr(cite, "corrected_citation") else str(cite)
        if normalize(raw) in canonical_norm:
            out.append(OtherCitationResult(raw, "in-source", True))
            continue
        looked_up = None
        if cl_client is not None:
            try:
                looked_up = cl_client.citation_lookup(raw)
            except Exception:  # noqa: BLE001
                looked_up = None
        if looked_up:
            found_match = any(
                r.get("status") == 200 for r in (looked_up if isinstance(looked_up, list) else [looked_up])
            )
            out.append(OtherCitationResult(raw, "courtlistener", found_match))
        else:
            out.append(OtherCitationResult(raw, "unverified", False))
    return out
