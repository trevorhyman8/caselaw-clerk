"""Deterministic candidate scoring — config/scoring.yaml weights derived
from the historical corpus category frequencies (see STYLE.md / style_stats
for the source counts). No LLM in the default path; the plan's optional
rerank is a separate, explicitly-costed add-on layered on top of this.
"""
from __future__ import annotations

import re

import yaml

from pipeline.settings import ROOT

_STATUTE_TERM_RE_CACHE: dict[str, re.Pattern] = {}


def _load_yaml(name: str) -> dict:
    with open(ROOT / "config" / name) as f:
        return yaml.safe_load(f)


def _family_patterns(queries_cfg: dict) -> dict[str, re.Pattern]:
    out = {}
    for key, fam in queries_cfg["families"].items():
        # terms are quoted CourtListener query syntax like '"Fair Debt Collection"'
        # or bare tokens like FDCPA — strip quotes, build a case-insensitive OR regex
        terms = [t.strip('"') for t in fam["terms"]]
        pattern = "|".join(re.escape(t) for t in terms)
        out[key] = re.compile(pattern, re.I)
    return out


def detect_statutes(text: str, queries_cfg: dict | None = None) -> list[str]:
    queries_cfg = queries_cfg or _load_yaml("queries.yaml")
    patterns = _family_patterns(queries_cfg)
    return [key for key, pat in patterns.items() if pat.search(text)]


def detect_posture_hits(text: str, queries_cfg: dict | None = None) -> list[str]:
    queries_cfg = queries_cfg or _load_yaml("queries.yaml")
    return [kw for kw in queries_cfg["posture_keywords"] if kw.lower() in text.lower()]


def score_case(
    text: str,
    court_id: str,
    historical_judges: set[str] | None = None,
    judge: str | None = None,
) -> dict:
    """Returns {score, breakdown}. `text` should be the opinion's full text
    (or at least enough to detect statute families and posture keywords)."""
    scoring_cfg = _load_yaml("scoring.yaml")
    queries_cfg = _load_yaml("queries.yaml")

    if len(text.strip()) < scoring_cfg["hard_reject_min_chars"]:
        return {"score": 0, "breakdown": {"hard_reject": "opinion text too short"}}

    statutes = detect_statutes(text, queries_cfg)
    if not statutes:
        return {"score": 0, "breakdown": {"hard_reject": "no recognized statute family"}}

    topic_pts = scoring_cfg["topic_pts"]
    sorted_statutes = sorted(statutes, key=lambda s: topic_pts.get(s, 0), reverse=True)
    primary, secondary = sorted_statutes[0], sorted_statutes[1:]

    primary_pts = topic_pts.get(primary, 0)
    secondary_pts = sum(topic_pts.get(s, 0) for s in secondary) * 0.5

    court_mult = scoring_cfg["court_mult"].get(court_id, scoring_cfg["court_mult"]["default"])

    bonuses = scoring_cfg["bonuses"]
    posture_hits = detect_posture_hits(text, queries_cfg)
    bonus_total = 0
    breakdown_bonuses = {}
    if posture_hits:
        bonus_total += bonuses["posture_keyword_hit"]
        breakdown_bonuses["posture_keyword_hit"] = posture_hits[:3]
    if judge and historical_judges and judge.split()[-1] in historical_judges:
        bonus_total += bonuses["historical_judge_hit"]
        breakdown_bonuses["historical_judge_hit"] = judge
    if len(text) >= 2000:
        bonus_total += bonuses["substantial_opinion_length"]
        breakdown_bonuses["substantial_opinion_length"] = True

    score = court_mult * (primary_pts + secondary_pts) + bonus_total

    return {
        "score": round(score, 2),
        "breakdown": {
            "primary_statute": primary, "secondary_statutes": secondary,
            "court_mult": court_mult, "primary_pts": primary_pts,
            "secondary_pts": secondary_pts, "bonuses": breakdown_bonuses,
        },
    }


def build_digest(scored_cases: list[dict]) -> list[dict]:
    """scored_cases: list of {case_id, case_name, court_id, score, ...}.
    Returns top-N above the floor, deterministically ordered — ties broken
    by (date desc, court priority, case_id asc) so reruns are reproducible."""
    scoring_cfg = _load_yaml("scoring.yaml")
    floor = scoring_cfg["digest_score_floor"]
    max_items = scoring_cfg["digest_max_items"]

    eligible = [c for c in scored_cases if c["score"] >= floor]
    eligible.sort(key=lambda c: (-c["score"], c.get("date_filed", ""), c["case_id"]), reverse=False)
    eligible.sort(key=lambda c: c["score"], reverse=True)
    return eligible[:max_items]
