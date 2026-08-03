"""CourtListener v4 client.

Verified live (2026-08-02, unauthenticated):
  - GET /search/?type=o|r&q=...&court=...  WORKS without a token (rate-limited
    harder: 5/min, 50/hr, 125/day free-tier ceiling — see TokenBucket below).
    Returns cluster_id/docket_id, court, dateFiled, docketNumber, and each
    opinion's `download_url` — a direct link to the COURT'S OWN official PDF.
  - GET /opinions/{id}/ and /clusters/{id}/ REQUIRE a token (403 without one)
    — they return the CourtListener-hosted text/HTML.
  - POST /citation-lookup/ REQUIRES a token.

Design decision: don't depend on the authenticated detail endpoints for
opinion TEXT at all. Fetch `download_url` directly (bypasses CourtListener's
auth wall entirely, and is arguably better evidence — it's the court's own
PDF, not a CourtListener transcription). A token only becomes necessary for
(a) higher rate limits, (b) RECAP search alerts, (c) the Citation Lookup API
used in the verification gate's layer-2 cross-check. All three degrade
gracefully without one; §2 of the coverage backtest (verify/v1) reports the
actual effect of running token-less.
"""
from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone

import httpx
import pdfplumber

from pipeline.db import db
from pipeline.settings import settings

BASE = "https://www.courtlistener.com/api/rest/v4"


class RateLimitExceeded(RuntimeError):
    pass


class TokenBucket:
    """Persisted daily budget per source (pipeline.db.api_budget), plus an
    in-process minute/hour throttle. Free-tier ceiling: 5/min, 50/hr,
    125/day. A token raises these (Free Law Project membership); the bucket
    is configured conservatively either way and never assumes the higher
    limit — it counts actual calls and refuses once the tracked ceiling is
    hit, so the daily sweep degrades to "budget exhausted, try tomorrow"
    instead of getting the account rate-limited or banned."""

    def __init__(self, per_min: int = 5, per_day: int = 120, source: str = "courtlistener"):
        self.per_min = per_min
        self.per_day = per_day
        self.source = source
        self._minute_calls: list[float] = []

    def _today(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _day_used(self) -> int:
        with db.get_conn() as conn:
            row = conn.execute(
                db.qmark("SELECT used FROM api_budget WHERE source = ? AND day = ?"),
                (self.source, self._today()),
            ).fetchone()
            return row[0] if row else 0

    def _record(self) -> None:
        with db.get_conn() as conn:
            day = self._today()
            existing = conn.execute(
                db.qmark("SELECT used FROM api_budget WHERE source = ? AND day = ?"),
                (self.source, day),
            ).fetchone()
            if existing:
                conn.execute(
                    db.qmark("UPDATE api_budget SET used = used + 1 WHERE source = ? AND day = ?"),
                    (self.source, day),
                )
            else:
                conn.execute(
                    db.qmark("INSERT INTO api_budget (source, day, used) VALUES (?, ?, 1)"),
                    (self.source, day),
                )

    def acquire(self) -> None:
        used_today = self._day_used()
        if used_today >= self.per_day:
            raise RateLimitExceeded(
                f"{self.source}: daily budget exhausted ({used_today}/{self.per_day}). "
                "Resumes tomorrow (UTC), or raise the limit by joining the Free Law "
                "Project membership + setting COURTLISTENER_TOKEN."
            )
        now = time.monotonic()
        self._minute_calls = [t for t in self._minute_calls if now - t < 60]
        if len(self._minute_calls) >= self.per_min:
            sleep_for = 60 - (now - self._minute_calls[0]) + 0.1
            time.sleep(max(sleep_for, 0))
        self._minute_calls.append(time.monotonic())
        self._record()


@dataclass
class OpinionHit:
    cluster_id: int
    docket_id: int | None
    case_name: str
    court_id: str
    court_label: str
    date_filed: str | None
    docket_number: str | None
    download_url: str | None
    absolute_url: str
    judge: str | None


class CourtListenerClient:
    def __init__(self, token: str | None = None, bucket: TokenBucket | None = None):
        self.token = token or settings.courtlistener_token
        self.bucket = bucket or TokenBucket(
            per_min=5, per_day=120 if not self.token else 1000
        )
        headers = {"Authorization": f"Token {self.token}"} if self.token else {}
        self._client = httpx.Client(base_url=BASE, headers=headers, timeout=30)

    def close(self) -> None:
        self._client.close()

    def search_opinions(
        self, query: str, courts: list[str] | None = None,
        filed_after: date | None = None, order_by: str = "dateFiled desc",
    ) -> list[OpinionHit]:
        self.bucket.acquire()
        params = {"type": "o", "q": query, "order_by": order_by}
        if courts:
            params["court"] = " ".join(courts)
        if filed_after:
            params["filed_after"] = filed_after.isoformat()
        r = self._client.get("/search/", params=params)
        r.raise_for_status()
        data = r.json()
        out = []
        for res in data.get("results", []):
            opinions = res.get("opinions") or [{}]
            out.append(
                OpinionHit(
                    cluster_id=res["cluster_id"],
                    docket_id=res.get("docket_id"),
                    case_name=res.get("caseName", ""),
                    court_id=res.get("court_id", ""),
                    court_label=res.get("court_citation_string", ""),
                    date_filed=res.get("dateFiled"),
                    docket_number=res.get("docketNumber"),
                    download_url=opinions[0].get("download_url"),
                    absolute_url="https://www.courtlistener.com" + res.get("absolute_url", ""),
                    judge=res.get("judge") or None,
                )
            )
        return out

    def search_recap(
        self, query: str, courts: list[str] | None = None,
        filed_after: date | None = None,
    ) -> list[dict]:
        """RECAP (PACER-mirror) search — the source for unpublished district
        court orders. Returns raw result dicts (schema is richer/messier than
        opinions; callers extract what they need). Requires a token for
        reliable results; membership unlocks RECAP search *alerts*."""
        self.bucket.acquire()
        params = {"type": "r", "q": query, "order_by": "dateFiled desc"}
        if courts:
            params["court"] = " ".join(courts)
        if filed_after:
            params["filed_after"] = filed_after.isoformat()
        r = self._client.get("/search/", params=params)
        r.raise_for_status()
        return r.json().get("results", [])

    def citation_lookup(self, citation_text: str) -> dict | None:
        """POST /citation-lookup/ — requires a token. Used by the
        verification gate (layer 2) as an anti-hallucination guardrail on
        any citation NOT already verified as a verbatim in-source quote.
        Returns None (not a hard error) when no token is configured, so
        callers can distinguish "not checked" from "checked, no match"."""
        if not self.token:
            return None
        self.bucket.acquire()
        r = self._client.post("/citation-lookup/", data={"text": citation_text})
        if r.status_code == 401:
            return None
        r.raise_for_status()
        return r.json()


def _strip_running_headers_footers(text: str) -> str:
    """Legal-opinion PDFs repeat a running header/footer on every page
    (e.g. "8 COFFEY V. FAST EASY OFFER, LLC"). pdfplumber extracts each
    page's text including that header, which then lands MID-SENTENCE at
    every page break in the joined text — breaking exact-match quote
    verification on any quote that happens to span a page boundary. Found
    via a real end-to-end test: a verbatim court quote failed layer-1
    verification purely because of this artifact, not because anything was
    actually wrong with the draft.

    Heuristic: any line (after stripping leading page numbers) that repeats
    3+ times across the document and is short (<100 chars) is almost
    certainly a running header/footer, not body text — real body sentences
    essentially never repeat verbatim 3+ times in a single opinion."""
    lines = text.split("\n")
    normalized_counts: dict[str, int] = {}
    for line in lines:
        stripped = re.sub(r"^\s*\d{1,4}\s*", "", line).strip()
        if stripped and len(stripped) < 100:
            normalized_counts[stripped] = normalized_counts.get(stripped, 0) + 1

    repeated = {k for k, v in normalized_counts.items() if v >= 3}
    kept = []
    for line in lines:
        stripped = re.sub(r"^\s*\d{1,4}\s*", "", line).strip()
        if stripped in repeated:
            continue
        kept.append(line)
    return "\n".join(kept)


def fetch_opinion_text(download_url: str, timeout: int = 30) -> tuple[bytes, str]:
    """Fetch the court's own official PDF (bypasses CourtListener's auth
    wall on opinion-detail entirely) and extract text. Returns
    (raw_pdf_bytes, extracted_text). Raises httpx.HTTPStatusError on a dead
    link — old download_urls from courts that reorganized their document
    servers do 404 (observed for a 2017 2d Cir. url during dev); callers
    should treat that as "artifact unavailable from this source", not a
    pipeline bug, and fall back to GovInfo/RECAP for older cases."""
    r = httpx.get(download_url, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    content = r.content
    text = ""
    if download_url.lower().endswith(".pdf") or r.headers.get("content-type", "").startswith(
        "application/pdf"
    ):
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        text = _strip_running_headers_footers(text)
    else:
        text = r.text
    return content, text
