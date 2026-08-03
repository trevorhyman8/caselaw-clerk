"""Content agent — drafts a new post in Scott's voice. Structured output
only, evidence-scoped by construction: the prompt supplies STYLE.md (voice
rules), the fact sheet (already self-verified against the source at ingest
time — see pipeline/verify_gate/facts.py), the exemplars (real past posts,
retrieved by pipeline/style/exemplars.py), and the opinion text itself — and
explicitly forbids drawing on any legal knowledge not in that context. This
prompt design is the generation-side half of the anti-hallucination
strategy; pipeline/verify_gate is the audit-side half.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline import llm
from pipeline.style.exemplars import Exemplar, render_exemplar_block
from pipeline.verify_gate.facts import FactSheet

ROOT = Path(__file__).resolve().parent.parent.parent
STYLE_MD_PATH = ROOT / "STYLE.md"

SYSTEM_TEMPLATE = """{style_md}

HARD RULES (never violate these, no exceptions):
1. Every quoted passage in a "quote" block MUST be copied verbatim (character-for-character) from the <opinion> text you are given below. You may shorten a quote with " . . . " and mark a substitution with square brackets, but every retained word must appear in the source, in the same order.
2. You have NO permission to state any legal proposition, case name, citation, holding, or characterization that does not appear in the <opinion> or <fact_sheet> below. Do not use legal knowledge from your training. None. If you are unsure whether something is supported, leave it out.
3. Your own commentary is limited to the intro field and at most {max_bridges} short "bridge" blocks between quotes. Bridges preview or connect quotes procedurally — they never argue, evaluate, or predict (see STYLE.md's Hard Prohibitions).
4. Categories must be chosen ONLY from the frozen vocabulary list provided below. Never invent a new category.
5. Output ONLY the JSON object described below. No markdown fences, no commentary.

Output JSON shape:
{{
  "title": "declarative, court/judge-prefixed, present-tense, states the outcome (see STYLE.md Title conventions)",
  "slug": "url-slug-like-this",
  "intro": "1-3 sentences per STYLE.md's intro templates, naming the court/judge/posture and the holding direction",
  "blocks": [{{"type": "quote"|"bridge", "text": "..."}}],
  "closing_citation": "per STYLE.md's closing citation format",
  "categories": ["from the frozen vocabulary only"]
}}"""

USER_TEMPLATE = """<fact_sheet>
posture: {posture}
holding_direction: {holding_direction}
statutes: {statutes}
key_passages:
{key_passages}
</fact_sheet>

<case_meta>
{case_meta}
</case_meta>

<opinion sha256="{artifact_sha256}">
{opinion_text}
</opinion>

<exemplars>
{exemplars}
</exemplars>

<frozen_category_vocabulary>
{categories}
</frozen_category_vocabulary>

{angle_line}Write the post now, in Scott Hyman's voice, following every hard rule above."""


def build_prompt(
    case_meta: dict,
    opinion_text: str,
    fact_sheet: FactSheet,
    exemplars: list[Exemplar],
    frozen_categories: list[str],
    artifact_sha256: str,
    angle: str | None = None,
    max_bridges: int = 2,
) -> tuple[str, str]:
    style_md = STYLE_MD_PATH.read_text()
    system = SYSTEM_TEMPLATE.format(style_md=style_md, max_bridges=max_bridges)

    key_passages = "\n".join(f"- {p.get('text', '')[:400]}" for p in fact_sheet.key_passages)
    exemplar_text = "\n\n".join(render_exemplar_block(e) for e in exemplars) or "(none retrieved)"
    angle_line = f"Suggested angle/hook: {angle}\n\n" if angle else ""

    user = USER_TEMPLATE.format(
        posture=fact_sheet.posture, holding_direction=fact_sheet.holding_direction,
        statutes=fact_sheet.statutes, key_passages=key_passages, case_meta=case_meta,
        artifact_sha256=artifact_sha256, opinion_text=opinion_text[:45000],
        exemplars=exemplar_text, categories=frozen_categories, angle_line=angle_line,
    )
    return system, user


def draft_post(
    case_meta: dict,
    opinion_text: str,
    fact_sheet: FactSheet,
    exemplars: list[Exemplar],
    frozen_categories: list[str],
    artifact_sha256: str,
    angle: str | None = None,
) -> dict:
    system, user = build_prompt(
        case_meta, opinion_text, fact_sheet, exemplars, frozen_categories, artifact_sha256, angle
    )
    data = llm.complete_json(user, system=system)
    required = {"title", "slug", "intro", "blocks", "closing_citation", "categories"}
    missing = required - set(data.keys())
    if missing:
        raise ValueError(f"draft missing required fields: {missing}")
    return data
