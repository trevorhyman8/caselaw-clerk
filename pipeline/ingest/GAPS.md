# Secondary-source gaps found during Phase 1 dev (2026-08-02)

Verified working, live, unauthenticated:
- **CourtListener** search (opinions + RECAP) — `courtlistener.py`
- **CFPB newsroom RSS** — `agencies.py::fetch_cfpb`
- **California Courts** published + unpublished slip opinions (direct PDF
  links) — `calcourts.py`

Not wired, needs revisiting (not silently skipped — each raises or is
flagged so the gap is visible, not assumed covered):
- **FCC**: every RSS endpoint tried (`fcc.gov/feeds/headlines.xml`,
  `fcc.gov/news-events/headlines/rss`) returns 403 from their Akamai edge
  regardless of User-Agent. `agencies.fetch_fcc()` raises `NotImplementedError`.
  Next step: investigate FCC's EDOCS/ECFS APIs (developer registration may be
  required), or fall back to Scott manually forwarding FCC items he wants
  covered — same manual-source pattern as the Westlaw fallback.
- **9th Circuit RSS**: `cdn.ca9.uscourts.gov/datastore/opinions/rss.xml`
  (from config/queries.yaml) 404s. Low priority — ca9 coverage is already
  strong via CourtListener's opinion search (verified live against real
  current TCPA cases), so this was meant as pure redundancy, not a primary
  path. Needs the correct current URL if redundancy is wanted later.
- **GovInfo USCOURTS weekly reconciliation**: designed in the plan as a
  free-tier backstop; not yet implemented. Needs `GOVINFO_API_KEY` (free,
  self-serve at api.data.gov) — a quick operator step, bundled into
  SETUP.md rather than blocking here.
- **CA Legislature (leginfo) bill tracking**: per the plan, this is
  Scott-driven ("track this specific bill") rather than auto-discovered —
  not built as an automated sweep in v1.

## PDF text-extraction fidelity at page boundaries (found via a real end-to-end test, 2026-08-02)

Running a full case through the pipeline (Coffey v. Fast Easy Offer, LLC, a
real, freshly-fetched 9th Cir. opinion) surfaced a genuine PDF-extraction
issue: `pdfplumber` occasionally drops or garbles a few words exactly at a
page break (a phrase like "Defendants are" vanished between two pages in
one spot — not a hallucination, a lossy extraction). Running-header/footer
lines (e.g. "8 COFFEY V. FAST EASY OFFER, LLC" repeated on every page) are
now stripped (`courtlistener.py::_strip_running_headers_footers`, added
during this same test), which fixed most page-boundary quote breaks, but
does not fix outright word loss at a page seam.

**This is not a gap in the safety design — it's the safety design working
correctly under a real imperfection.** The layer-1 quote matcher correctly
refused to verify a quote it couldn't find character-for-character, and the
gate correctly routed the draft to NEEDS_REVIEW rather than silently
publishing a slightly-corrupted quote. The fix for the underlying
extraction fidelity (worth doing before heavy production use) is to try an
alternative extraction path when pdfplumber's text has a suspiciously
abrupt page-boundary sentence — e.g. `pypdfium2`'s raw text layer, or
falling back to the fuzzy-match tier with a wider window specifically
across page-break offsets. Not built yet; flagged rather than silently
accepted.
