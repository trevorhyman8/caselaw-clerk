"""Canonical text normalization — N(s). Applied identically to the source
opinion text and to a draft's quoted segments before comparison, so quote
matching isn't defeated by cosmetic differences (smart quotes, dashes,
whitespace) while still catching a genuinely altered quote.

Order matters and is fixed here — always normalize both sides with the exact
same pipeline, never one side only.
"""
from __future__ import annotations

import re
import unicodedata

_WS_RE = re.compile(r"\s+")
# Star-pagination tokens (Westlaw page markers like "*11") and footnote
# markers Scott's historical corpus carries from Westlaw renderings
# (`^3^`-style, or bare superscript-looking trailing digits attached to a
# word) — stripped only when isolated as their own token, never inside a
# real citation's pincite ("at *3"), which normalize() is not responsible
# for touching (pincites live in the citation string, not inside a quote
# segment being checked for verbatim-ness).
_STAR_PAGE_RE = re.compile(r"(?<!\w)\*\d{1,4}(?!\w)")
_FOOTNOTE_CARET_RE = re.compile(r"\^\d{1,3}\^")


def normalize(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = (
        s.replace("“", '"').replace("”", '"')
        .replace("‘", "'").replace("’", "'")
        .replace("–", "-").replace("—", "-")
        .replace("…", "...")
        .replace(" ", " ")
        .replace("­", "")  # soft hyphen
    )
    s = _FOOTNOTE_CARET_RE.sub("", s)
    s = _STAR_PAGE_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def normalize_casefold(s: str) -> str:
    return normalize(s).casefold()
