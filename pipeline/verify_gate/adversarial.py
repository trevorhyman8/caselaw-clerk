"""Layer 4 — adversarial LLM verifier. Fresh context every time: no
STYLE.md, no exemplars, nothing that could make the model sympathetic to the
draft it's reviewing. Its instructions are to find fault, not to help. This
layer can only FAIL a draft that layers 1-3 already passed — it never
rescues a draft those layers rejected, and a clean verdict from this layer
alone is not sufficient (all four layers must pass).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from pipeline import llm

VERIFIER_SYSTEM_PROMPT = """You are a hostile fact-checker reviewing a draft legal-blog post before publication on a law firm's website. You did NOT write this draft and have no stake in it looking good. Your only job is to find anything in the draft that is not directly and specifically supported by the court opinion text you are given.

Be pedantic. Treat every claim, characterization, case name, party name, date, docket detail, procedural fact, and implied outcome as something that must trace to the opinion text. A plausible-sounding claim that you cannot actually locate in the opinion text is a violation — do not give the draft the benefit of the doubt.

Do not evaluate writing quality, style, or whether you'd have written it differently. Only evaluate factual/textual support."""

VERIFIER_PROMPT = """<opinion>
{opinion_text}
</opinion>

<draft>
{draft_text}
</draft>

List EVERY claim, characterization, case reference, party name, date, docket number, or implied outcome in the draft that is NOT directly and specifically supported by the opinion text above. For each violation, quote the exact draft text and explain what's missing or wrong.

Return JSON:
{{"violations": [{{"draft_text": "...", "why": "..."}}]}}

An empty violations list means the draft is fully supported by the opinion. Do not report a violation for text that is fair paraphrase of something the opinion clearly states — only report claims that go beyond, distort, or cannot be traced to the opinion."""


@dataclass
class Violation:
    draft_text: str
    why: str


@dataclass
class AdversarialResult:
    violations: list[Violation] = field(default_factory=list)
    prompt_sha256: str = ""

    @property
    def passed(self) -> bool:
        return len(self.violations) == 0


def render_draft_text(draft: dict) -> str:
    parts = [draft.get("intro", "")]
    for block in draft.get("blocks", []):
        parts.append(block.get("text", ""))
    parts.append(draft.get("closing_citation", "") or "")
    return "\n\n".join(p for p in parts if p)


def verify(opinion_text: str, draft: dict) -> AdversarialResult:
    draft_text = render_draft_text(draft)
    prompt = VERIFIER_PROMPT.format(opinion_text=opinion_text[:40000], draft_text=draft_text)
    prompt_sha = hashlib.sha256((VERIFIER_SYSTEM_PROMPT + prompt).encode()).hexdigest()

    data = llm.complete_json(prompt, system=VERIFIER_SYSTEM_PROMPT)
    violations = [Violation(v.get("draft_text", ""), v.get("why", "")) for v in data.get("violations", [])]
    return AdversarialResult(violations=violations, prompt_sha256=prompt_sha)
