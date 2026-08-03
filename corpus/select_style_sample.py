"""Select a stratified sample (era x statute-family) of Scott's posts for the
one-time LLM voice-distillation pass (see build_style_workflow output ->
STYLE.md). Writes corpus/style_sample.json — a list of batches, each with a
handful of posts, sized for a single subagent's context."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

CORPUS_DB = Path(__file__).resolve().parent / "corpus.sqlite"
OUT = Path(__file__).resolve().parent / "style_sample.json"

ERA_BUCKETS = [("2008", "2012"), ("2013", "2017"), ("2018", "2020"), ("2021", "2024")]
FAMILIES = ["fdcpa", "tcpa", "fcra", "privacy", "arbitration", "class", "ucl", "regulatory"]
PER_STRATUM = 5
BATCH_SIZE = 15


def era_of(date_str: str) -> tuple[str, str] | None:
    year = date_str[:4]
    for lo, hi in ERA_BUCKETS:
        if lo <= year <= hi:
            return (lo, hi)
    return None


def main() -> None:
    conn = sqlite3.connect(CORPUS_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT date, url, categories, content, statutes, postures, court_id "
        "FROM posts WHERE author = 'Scott J. Hyman' AND length(content) BETWEEN 1200 AND 6000"
    ).fetchall()
    conn.close()

    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if not r["date"]:
            continue
        era = era_of(r["date"])
        if not era:
            continue
        statutes = json.loads(r["statutes"] or "[]")
        primary = next((f for f in FAMILIES if f in statutes), None)
        if not primary:
            continue
        buckets[(era, primary)].append(dict(r))

    sample = []
    for key, posts in buckets.items():
        posts.sort(key=lambda p: p["date"])  # deterministic selection
        step = max(1, len(posts) // PER_STRATUM)
        sample.extend(posts[::step][:PER_STRATUM])

    print(f"selected {len(sample)} posts across {len(buckets)} strata")

    batches = [sample[i : i + BATCH_SIZE] for i in range(0, len(sample), BATCH_SIZE)]
    OUT.write_text(json.dumps(batches, indent=2, ensure_ascii=False, default=str))
    print(f"wrote {len(batches)} batches to {OUT}")


if __name__ == "__main__":
    main()
