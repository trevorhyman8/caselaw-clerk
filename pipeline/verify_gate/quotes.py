"""Layer 1 — quote integrity. Every quoted passage in a draft must be an
exact (normalized) substring of the source opinion text, in document order.
This is the spine of the whole anti-hallucination design: Scott's posts are
~93% verbatim court language (per STYLE.md), so verifying quotes verbatim
covers the overwhelming majority of what could go wrong in a draft.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from pipeline.verify_gate.normalize import normalize

FUZZY_RATIO_MIN = 0.98

# Editorial ellipsis markers a quote may legitimately contain: "...", ". . .",
# or a bracketed "[...]". Splitting on these turns one long quote into
# multiple segments, each independently checked to appear (in order) in the
# source — the gap between segments is exactly where text was omitted.
_ELLIPSIS_SPLIT_RE = re.compile(r"\s*(?:\.\s*\.\s*\.|\[\s*\.\.\.\s*\])\s*")
# A bracketed alteration inside a quote, e.g. "[the plaintiff]" standing in
# for "he" in the original — courts and Scott both do this. Converted to a
# bounded wildcard so the matcher doesn't require the bracketed text itself
# to appear verbatim (it usually doesn't; that's the point of brackets).
_BRACKET_RE = re.compile(r"\[[^\]]{0,60}\]")


@dataclass
class SegmentResult:
    text: str
    tier: str  # exact | fuzzy | fail
    start: int | None = None
    end: int | None = None
    diff: str | None = None


@dataclass
class QuoteCheckResult:
    segments: list[SegmentResult] = field(default_factory=list)
    in_order: bool = True
    quote_share: float | None = None

    @property
    def all_exact(self) -> bool:
        return all(s.tier == "exact" for s in self.segments)

    @property
    def any_fail(self) -> bool:
        return any(s.tier == "fail" for s in self.segments)


def _segment_to_regex(segment: str) -> re.Pattern:
    """Turn a quote segment into a regex: escape everything, but let any
    bracketed alteration match up to 60 arbitrary characters, and let
    whitespace runs match any whitespace run (normalize() already collapsed
    the segment's own whitespace, but the regex needs the same flexibility
    against source text that hasn't been pre-joined identically)."""
    parts = _BRACKET_RE.split(segment)
    escaped = [re.escape(p) for p in parts]
    pattern = r".{0,80}?".join(escaped)
    pattern = re.sub(r"\\ ", r"\\s+", pattern)  # escaped spaces -> flexible whitespace
    return re.compile(pattern, re.DOTALL)


def _fuzzy_locate(segment: str, source: str) -> tuple[float, int, int, str] | None:
    """Sliding-window token-alignment fallback for near-miss quotes (PDF
    extraction artifacts: ligatures, line-break hyphenation). WARN tier only
    — never auto-passes a draft, always surfaced with a diff."""
    seg_tokens = segment.split()
    if not seg_tokens:
        return None
    window = max(len(seg_tokens), 3)
    src_tokens = source.split()
    best = (0.0, 0, 0, "")
    step = max(1, window // 4)
    for i in range(0, max(len(src_tokens) - window + 1, 1), step):
        candidate = " ".join(src_tokens[i : i + int(window * 1.3)])
        ratio = difflib.SequenceMatcher(None, segment, candidate).ratio()
        if ratio > best[0]:
            best = (ratio, i, i + int(window * 1.3), candidate)
    if best[0] >= FUZZY_RATIO_MIN:
        diff = "\n".join(
            difflib.unified_diff(
                segment.split(), best[3].split(), lineterm="", n=2
            )
        )
        return (best[0], best[1], best[2], diff)
    return None


def check_quote(quote_text: str, canonical_text: str, search_from: int = 0) -> QuoteCheckResult:
    """Check one quote block (which may itself contain internal ellipses)
    against the canonical source. `search_from` enforces document-order: a
    later quote in the same draft must not match earlier in the source than
    an earlier quote did (Scott never splices quotes out of order)."""
    canonical_norm = normalize(canonical_text)
    raw_segments = [s for s in _ELLIPSIS_SPLIT_RE.split(quote_text) if s.strip()]

    result = QuoteCheckResult()
    cursor = search_from
    for raw_seg in raw_segments:
        seg_norm = normalize(raw_seg)
        if not seg_norm:
            continue
        pattern = _segment_to_regex(seg_norm)
        m = pattern.search(canonical_norm, cursor)
        if m:
            result.segments.append(SegmentResult(raw_seg, "exact", m.start(), m.end()))
            cursor = m.start()  # allow slight backward slack for the next segment's own search_from below
            cursor = m.end()
            continue

        # try again from the top in case ordering assumption was wrong —
        # still exact-tier if found, but flag if it violates document order
        m2 = pattern.search(canonical_norm)
        if m2:
            in_order = m2.start() >= search_from
            result.segments.append(SegmentResult(raw_seg, "exact", m2.start(), m2.end()))
            if not in_order:
                result.in_order = False
            cursor = m2.end()
            continue

        fuzzy = _fuzzy_locate(seg_norm, canonical_norm)
        if fuzzy:
            ratio, start, end, diff = fuzzy
            result.segments.append(SegmentResult(raw_seg, "fuzzy", start, end, diff=diff))
            cursor = end
            continue

        result.segments.append(SegmentResult(raw_seg, "fail"))

    return result


def check_quote_blocks(
    quote_blocks: list[str], canonical_text: str, draft_body_chars: int | None = None
) -> QuoteCheckResult:
    """Check a full ordered list of quote blocks from a draft (each may be
    multi-segment). Enforces document order ACROSS blocks too.

    quote_share = (verbatim-verified chars) / (draft's OWN total body
    length), matching STYLE.md's historical norm (~0.7-0.9 of the POST is
    the court's own words) — NOT a fraction of the source opinion, which is
    typically 5-50x longer than the drafted excerpt and would make every
    real draft look like it barely quotes anything. Pass draft_body_chars
    explicitly (intro + all blocks, quote and bridge); omitting it falls
    back to quote-blocks-only length, which is a weaker proxy."""
    canonical_norm = normalize(canonical_text)
    combined = QuoteCheckResult()
    cursor = 0
    for block in quote_blocks:
        r = check_quote(block, canonical_text, search_from=cursor)
        combined.segments.extend(r.segments)
        if not r.in_order:
            combined.in_order = False
        exact_ends = [s.end for s in r.segments if s.end is not None]
        if exact_ends:
            cursor = max(exact_ends)

    quoted_chars = sum(len(s.text) for s in combined.segments if s.tier in ("exact", "fuzzy"))
    denominator = draft_body_chars if draft_body_chars else sum(len(b) for b in quote_blocks) or 1
    combined.quote_share = quoted_chars / denominator
    return combined
