"""Exemplar retrieval: given a new case's statutes/court/posture, find the
most-similar past Scott posts to inject as few-shot examples at draft time.

Backed by corpus/corpus.sqlite (built by corpus/load_corpus.py — the same
per-post table doubles as the exemplar index; no separate embedding store in
v1, per design: statute/category filter + court/posture preference + a light
lexical rank does most of the work without an embedding dependency).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

CORPUS_DB = Path(__file__).resolve().parent.parent.parent / "corpus" / "corpus.sqlite"


@dataclass
class Exemplar:
    date: str
    court_id: str | None
    postures: list[str]
    statutes: list[str]
    intro: str
    content: str
    closing_citation: str | None
    url: str | None


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(CORPUS_DB)
    conn.row_factory = sqlite3.Row
    return conn


def retrieve(
    statutes: list[str],
    court_id: str | None,
    posture: str | None,
    limit: int = 3,
    exclude_url: str | None = None,
) -> list[Exemplar]:
    """Rank Scott's past posts for this new case. Scoring (all in Python,
    not SQL, since corpus is only ~2,900 rows — a full scan is instant):
      +3 per overlapping statute family
      +2 same court_id
      +2 same primary posture
      +1 if post-2018 (style drift — prefer recent voice)
    Ties broken by recency. `exclude_url` supports leave-one-out mode for
    the backtest eval (a post must never retrieve itself as an exemplar).
    """
    conn = _conn()
    rows = conn.execute(
        "SELECT date, author, url, court_id, postures, statutes, intro, "
        "content, closing_citation FROM posts WHERE author = 'Scott J. Hyman' "
        "AND length(content) > 800"
    ).fetchall()
    conn.close()

    scored: list[tuple[float, dict]] = []
    for r in rows:
        if exclude_url and r["url"] == exclude_url:
            continue
        row_statutes = set(json.loads(r["statutes"] or "[]"))
        row_postures = json.loads(r["postures"] or "[]")
        score = 3 * len(row_statutes & set(statutes))
        if court_id and r["court_id"] == court_id:
            score += 2
        if posture and posture in row_postures:
            score += 2
        if r["date"] and str(r["date"]) >= "2018":
            score += 1
        if score > 0:
            scored.append((score, dict(r)))

    scored.sort(key=lambda t: (t[0], t[1]["date"] or ""), reverse=True)
    out = []
    for _, r in scored[:limit]:
        out.append(
            Exemplar(
                date=r["date"],
                court_id=r["court_id"],
                postures=json.loads(r["postures"] or "[]"),
                statutes=json.loads(r["statutes"] or "[]"),
                intro=r["intro"],
                content=r["content"],
                closing_citation=r["closing_citation"],
                url=r["url"],
            )
        )
    return out


def render_exemplar_block(ex: Exemplar) -> str:
    return (
        f"<exemplar date=\"{ex.date}\" court=\"{ex.court_id or 'unknown'}\">\n"
        f"{ex.content.strip()}\n"
        f"</exemplar>"
    )
