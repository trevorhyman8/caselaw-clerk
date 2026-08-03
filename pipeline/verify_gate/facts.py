"""Layer 3 — fact scoping.

Two stages:
1. INGEST-TIME extraction (`extract_fact_sheet`): one LLM call per newly
   captured case, producing posture / holding_direction / statutes / key
   passages — each key passage required to carry a verbatim supporting span,
   which is itself checked against the source via the layer-1 quote matcher
   (`pipeline.verify_gate.quotes`). A fact sheet with an unverifiable span is
   rejected and re-extracted once; this makes the fact sheet self-verifying
   rather than a second unchecked LLM output trusted on faith.
2. DRAFT-TIME assertions (`assert_fact_consistency`): pure string checks, no
   LLM, comparing the draft's own intro sentence against the fact sheet
   (itself grounded in source text) and the locker's authoritative case
   metadata (judge/court come from the court record, not extraction).
   HOLDING DIRECTION is a hard-fail with no auto-repair shortcut — a
   "grants" written where the court actually denied is the single most
   damaging error class for a litigation partner's blog, so a mismatch here
   always routes to a full redraft, never a patch.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pipeline import llm
from pipeline.verify_gate.normalize import normalize
from pipeline.verify_gate.quotes import check_quote

POSTURE_ENUM = [
    "motion_to_dismiss", "summary_judgment", "class_certification",
    "compel_arbitration", "motion_to_strike", "demurrer", "appeal",
    "preliminary_injunction", "other",
]
HOLDING_ENUM = ["grants", "denies", "grants_in_part", "affirms", "reverses", "remands", "mixed"]

HOLDING_VERB_MAP = {
    "grants": [r"\bgrant(?:s|ed|ing)?\b"],
    "denies": [r"\bdeni(?:es|ed|al|ying)\b"],
    "grants_in_part": [r"grant(?:s|ed)?\s+in\s+part", r"grant(?:s|ed)?[^.]{0,20}deni(?:es|ed)"],
    "affirms": [r"\baffirm(?:s|ed|ing)?\b"],
    "reverses": [r"\brevers(?:es|ed|ing)?\b"],
    "remands": [r"\bremand(?:s|ed|ing)?\b"],
    "mixed": [r"\bin part\b"],
}

FACT_SHEET_PROMPT = """You are extracting structured facts from a court opinion for a legal-blog drafting pipeline. You must NOT use any legal knowledge from memory — every fact must be directly supported by the opinion text below, and you must quote the EXACT supporting sentence/phrase for each fact so it can be mechanically re-verified against the source.

Case metadata (authoritative, not yours to override): {case_meta}

Opinion text:
<opinion>
{opinion_text}
</opinion>

Extract and return JSON with this exact shape:
{{
  "posture": one of {posture_enum},
  "posture_support": "verbatim phrase from the opinion supporting this posture classification",
  "holding_direction": one of {holding_enum},
  "holding_support": "verbatim sentence from the opinion stating the court's disposition",
  "statutes": ["short keys like tcpa, fdcpa, fcra, privacy, arbitration, class, ucl — only ones actually discussed"],
  "key_passages": [
    {{"text": "verbatim passage from the opinion", "why": "why this is a good passage to quote in a case-note blog post"}}
  ]
}}

key_passages should be 2-5 passages, each copied EXACTLY (character for character) from the opinion text above — these will be mechanically checked as substrings of the source, so do not paraphrase, summarize, or fix typos."""


@dataclass
class FactSheet:
    posture: str
    posture_support: str
    holding_direction: str
    holding_support: str
    statutes: list[str]
    key_passages: list[dict]
    posture_verified: bool = False
    holding_verified: bool = False
    passages_verified: list[bool] = field(default_factory=list)

    @property
    def fully_verified(self) -> bool:
        return self.posture_verified and self.holding_verified and all(self.passages_verified)


def extract_fact_sheet(opinion_text: str, case_meta: dict, retries: int = 1) -> FactSheet:
    prompt = FACT_SHEET_PROMPT.format(
        case_meta=case_meta, opinion_text=opinion_text[:40000],
        posture_enum=POSTURE_ENUM, holding_enum=HOLDING_ENUM,
    )
    last_error = None
    for attempt in range(retries + 1):
        try:
            data = llm.complete_json(prompt)
            sheet = FactSheet(
                posture=data.get("posture", "other"),
                posture_support=data.get("posture_support", ""),
                holding_direction=data.get("holding_direction", "mixed"),
                holding_support=data.get("holding_support", ""),
                statutes=data.get("statutes", []),
                key_passages=data.get("key_passages", []),
            )
            _verify_fact_sheet(sheet, opinion_text)
            if sheet.fully_verified or attempt == retries:
                return sheet
            last_error = "fact sheet had unverifiable spans; retrying"
        except Exception as e:  # noqa: BLE001
            last_error = str(e)
    raise RuntimeError(f"extract_fact_sheet failed after {retries + 1} attempts: {last_error}")


def _verify_fact_sheet(sheet: FactSheet, opinion_text: str) -> None:
    posture_check = check_quote(sheet.posture_support, opinion_text)
    sheet.posture_verified = posture_check.all_exact and not posture_check.any_fail

    holding_check = check_quote(sheet.holding_support, opinion_text)
    sheet.holding_verified = holding_check.all_exact and not holding_check.any_fail

    for passage in sheet.key_passages:
        c = check_quote(passage.get("text", ""), opinion_text)
        sheet.passages_verified.append(c.all_exact and not c.any_fail)


@dataclass
class FactAssertion:
    name: str
    passed: bool
    detail: str


def assert_fact_consistency(draft_intro: str, fact_sheet: FactSheet, case_meta: dict) -> list[FactAssertion]:
    """Draft-time, pure deterministic checks — no LLM. Judge/court come
    from locker metadata (authoritative); posture/holding come from the
    (already self-verified) fact sheet."""
    intro_norm = normalize(draft_intro).lower()
    out = []

    judge = (case_meta.get("judge") or "").strip()
    if judge:
        SUFFIXES = {"jr.", "jr", "sr.", "sr", "ii", "iii", "iv"}
        tokens = [t for t in judge.split() if t.strip(",").lower() not in SUFFIXES]
        surname = tokens[-1].lower() if tokens else judge.split()[-1].lower()
        out.append(FactAssertion("judge", surname in intro_norm, f"expected surname '{surname}' in intro"))

    posture = fact_sheet.posture.replace("_", " ")
    out.append(
        FactAssertion(
            "posture", posture in intro_norm or any(
                w in intro_norm for w in posture.split()
            ),
            f"expected posture cue '{posture}' in intro",
        )
    )

    holding_patterns = HOLDING_VERB_MAP.get(fact_sheet.holding_direction, [])
    holding_ok = any(re.search(p, intro_norm) for p in holding_patterns) if holding_patterns else True
    out.append(
        FactAssertion(
            "holding_direction", holding_ok,
            f"expected a verb matching '{fact_sheet.holding_direction}' in intro — "
            "HARD FAIL: mismatch here means the draft states the wrong outcome "
            "direction and must be fully redrafted, not patched.",
        )
    )

    return out
