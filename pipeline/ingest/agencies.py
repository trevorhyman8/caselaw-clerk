"""CFPB + FCC regulatory feeds — covers Scott's "Regulatory Agencies"
category (287 historical posts). These are agency actions, not court
opinions, so they never come from CourtListener.

Verified live 2026-08-02:
  - CFPB newsroom RSS works cleanly, unauthenticated: feed.consumerfinance.gov/about-us/newsroom/feed/
  - FCC's own RSS endpoints (fcc.gov/feeds/headlines.xml,
    fcc.gov/news-events/headlines/rss) return 403 from their Akamai edge for
    non-browser clients, including with a browser User-Agent — this held for
    every URL pattern tried during dev. NOT wired in v1; ingest_fcc() raises
    NotImplementedError so a caller can't silently believe it's covered. See
    SETUP.md "known gaps" — options to revisit: FCC's EDOCS search API
    (requires investigating their developer docs further), or a Google Alert
    / manual-forward path for FCC actions in the meantime, same pattern as
    the Westlaw manual-forward fallback for court decisions.
"""
from __future__ import annotations

from dataclasses import dataclass

import feedparser

CFPB_NEWSROOM_RSS = "https://www.consumerfinance.gov/about-us/newsroom/feed/"


@dataclass
class AgencyItem:
    title: str
    url: str
    published: str | None
    summary: str | None
    categories: list[str]
    agency: str


def fetch_cfpb(limit: int = 20) -> list[AgencyItem]:
    feed = feedparser.parse(CFPB_NEWSROOM_RSS)
    out = []
    for entry in feed.entries[:limit]:
        out.append(
            AgencyItem(
                title=entry.get("title", ""),
                url=entry.get("link", ""),
                published=entry.get("published"),
                summary=entry.get("summary"),
                categories=[t["term"] for t in entry.get("tags", [])] if entry.get("tags") else [],
                agency="cfpb",
            )
        )
    return out


def fetch_fcc(limit: int = 20) -> list[AgencyItem]:
    raise NotImplementedError(
        "FCC RSS is blocked (403) from every endpoint tried during dev — see module "
        "docstring. Not silently skipped: this raises so callers surface the gap "
        "rather than reporting false full coverage."
    )


if __name__ == "__main__":
    for item in fetch_cfpb(limit=5):
        print(item)
