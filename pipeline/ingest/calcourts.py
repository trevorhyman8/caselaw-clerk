"""California Courts of Appeal + Supreme Court daily slip opinions —
verified live 2026-08-02: courts.ca.gov is server-rendered (no JS required),
lists both published AND unpublished/non-citable opinions with a direct PDF
link per entry. This is the coverage-gap closer for unpublished Cal. Ct.
App. decisions, which CourtListener's opinion collection is weak on (they
depend on court-by-court submission pipelines that lag or skip unpublished
state appellate opinions).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

PUBLISHED_URL = "https://www.courts.ca.gov/opinions/publishedcitable-opinions"
UNPUBLISHED_URL = "https://www.courts.ca.gov/opinions/unpublishednon-citable-opinions"
UA = "Mozilla/5.0 (compatible; caselaw-clerk research bot)"

DIVISION_RE = re.compile(r"(\d)(?:st|nd|rd|th)\s+District Court of Appeal")


@dataclass
class CalCourtsOpinion:
    case_number: str
    case_name: str
    date_filed: str | None
    court_label: str
    pdf_url: str | None
    published: bool


def _court_id_from_label(label: str) -> str:
    if "Supreme Court" in label:
        return "cal"
    return "calctapp"


def fetch(published: bool = True, limit: int = 50) -> list[CalCourtsOpinion]:
    url = PUBLISHED_URL if published else UNPUBLISHED_URL
    r = httpx.get(url, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    out = []
    for block in soup.select(".result-excerpt")[:limit]:
        brow_primary = block.select_one(".result-excerpt__brow-primary")
        brow_secondary = block.select_one(".result-excerpt__brow-secondary")
        brow_notation = block.select_one(".result-excerpt__brow-notation")
        title_link = block.select_one(".result-excerpt__heading a")
        pdf_link = block.find("a", string=re.compile(r"^\s*PDF\s*$"))

        if not (brow_primary and title_link):
            continue
        case_number = brow_primary.get_text(strip=True)
        raw_title = title_link.get_text(strip=True)
        # heading text is "Party v. Party 7/31/26 CA2/1" — strip the trailing
        # date/division tag, which we already have structured from brow_*
        case_name = re.sub(
            r"\s+(?:\d{1,2}/\d{1,2}/\d{2,4}\s+)?CA\d(?:/\d)?(?:\s+filed\s+\d{1,2}/\d{1,2}/\d{2,4})?\s*$",
            "", raw_title,
        ).strip()
        court_label = brow_notation.get_text(" ", strip=True) if brow_notation else ""

        out.append(
            CalCourtsOpinion(
                case_number=case_number,
                case_name=case_name,
                date_filed=brow_secondary.get_text(strip=True) if brow_secondary else None,
                court_label=court_label,
                pdf_url=pdf_link["href"] if pdf_link and pdf_link.get("href") else None,
                published=published,
            )
        )
    return out


def to_court_id(op: CalCourtsOpinion) -> str:
    return _court_id_from_label(op.court_label)


if __name__ == "__main__":
    for op in fetch(published=True, limit=5):
        print(op)
    print("---unpublished---")
    for op in fetch(published=False, limit=5):
        print(op)
