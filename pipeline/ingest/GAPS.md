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
