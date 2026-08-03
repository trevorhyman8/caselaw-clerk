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

# Found via a real false positive: Montera v. Premier Nutrition — a
# supplement-labeling false-advertising class action with no relation to
# Scott's practice — scored 27.1 (well above the digest floor of 6) because
# its 70,000-char opinion CITES a TCPA case and an FCRA case as precedent
# for unrelated damages/interest reasoning. Full-text keyword search can't
# tell "this case is about X" from "this case cites a case about X."
#
# The fix is NOT an industry filter (tried that first: requiring a
# bank/lender/privacy keyword nearby — but it wrongly zeroed out Coffey v.
# Fast Easy Offer and Howard v. RNC, both genuine TCPA cases against a real
# estate company and a political committee respectively. TCPA defense is
# core to Scott's practice regardless of the defendant's industry; robocall
# law doesn't care who's calling). The actual fix: courts state what a case
# is ABOUT right at the start — case caption, then a summary/opening
# paragraph — so a statute family only counts if it shows up in that
# framing. A citation to unrelated precedent, buried deep in the analysis,
# doesn't. Verified: Montera's TCPA/FCRA mentions were both well past this
# window; Coffey's and Howard's were in the first ~800 characters.
EARLY_WINDOW_CHARS = 3000


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


def detect_statutes(text: str, queries_cfg: dict | None = None, case_name: str = "") -> list[str]:
    """Only counts a family if it appears in the case's OWN framing — the
    caption plus the opinion's opening ~3000 chars, where courts state what
    a case is actually about — not anywhere in the full text, which for a
    long opinion is mostly other cases' holdings being cited as precedent.
    See EARLY_WINDOW_CHARS's comment for why this replaced a naive
    full-text search."""
    queries_cfg = queries_cfg or _load_yaml("queries.yaml")
    patterns = _family_patterns(queries_cfg)
    haystack = f"{case_name}\n{text[:EARLY_WINDOW_CHARS]}"
    return [key for key, pat in patterns.items() if pat.search(haystack)]


def detect_posture_hits(text: str, queries_cfg: dict | None = None) -> list[str]:
    queries_cfg = queries_cfg or _load_yaml("queries.yaml")
    return [kw for kw in queries_cfg["posture_keywords"] if kw.lower() in text.lower()]


def score_case(
    text: str,
    court_id: str,
    case_name: str = "",
    historical_judges: set[str] | None = None,
    judge: str | None = None,
) -> dict:
    """Returns {score, breakdown}. `text` should be the opinion's full text
    (or at least enough to detect statute families and posture keywords)."""
    scoring_cfg = _load_yaml("scoring.yaml")
    queries_cfg = _load_yaml("queries.yaml")

    if len(text.strip()) < scoring_cfg["hard_reject_min_chars"]:
        return {"score": 0, "breakdown": {"hard_reject": "opinion text too short"}}

    statutes = detect_statutes(text, queries_cfg, case_name=case_name)
    if not statutes:
        return {
            "score": 0,
            "breakdown": {
                "hard_reject": "no recognized statute family in the case's own framing "
                "(caption + opening ~3000 chars) — a full-text match elsewhere is usually a "
                "citation to unrelated precedent, not the case's actual subject"
            },
        }
    if not any(s in scoring_cfg["core_families"] for s in statutes):
        # "class"/"regulatory" alone means "some class action" or "some
        # agency proceeding" — true of thousands of cases with nothing to
        # do with Scott's practice. Found live: Montera v. Premier
        # Nutrition (a supplement-labeling class action) legitimately
        # mentions class certification in its own opening, so the
        # early-window fix alone let it through at a lower score. Real
        # relevance requires one of his actual named statutes (config's
        # core_families) to be present — class/regulatory/ucl remain valid
        # score BOOSTERS below (secondary_pts) but can't carry a case alone.
        return {
            "score": 0,
            "breakdown": {
                "hard_reject": f"only non-core families matched ({statutes}) — needs at least "
                "one of Scott's actual named statutes (config/scoring.yaml core_families), not "
                "just a generic class-action/regulatory posture"
            },
        }

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
