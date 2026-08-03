"""Phase 0 deterministic pass: load the private xlsx corpus, extract
structural facts (intro templates, posture, court, judges, closing-citation
grammar, category vocabulary, title conventions) with zero LLM calls, and
write:
  - corpus/corpus.sqlite    per-post structured rows (feeds the exemplar
                              index in build_exemplars.py)
  - corpus/style_stats.json  aggregate frequency tables (feeds STYLE.md)

Run: uv run python -m corpus.load_corpus [--xlsx PATH] [--author "Scott J. Hyman"]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_XLSX = ROOT / ".private" / "corpus.xlsx"
DB_PATH = ROOT / "corpus" / "corpus.sqlite"
STATS_PATH = ROOT / "corpus" / "style_stats.json"

# --- court detection -------------------------------------------------------
# Ordered so more specific patterns (district courts) are tried before the
# generic "Cal." patterns that could otherwise false-match "C.D. Cal."
COURT_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    ("cacd", "C.D. Cal.", re.compile(r"C\.\s?D\.\s?Cal", re.I)),
    ("cand", "N.D. Cal.", re.compile(r"N\.\s?D\.\s?Cal", re.I)),
    ("casd", "S.D. Cal.", re.compile(r"S\.\s?D\.\s?Cal", re.I)),
    ("caed", "E.D. Cal.", re.compile(r"E\.\s?D\.\s?Cal", re.I)),
    ("ca9", "9th Cir.", re.compile(r"9th\s?Cir|Ninth Circuit", re.I)),
    ("scotus", "U.S. Supreme Court", re.compile(r"\bS\.\s?Ct\.|Supreme Court of the United States", re.I)),
    ("calctapp", "Cal. Ct. App.", re.compile(r"Cal\.\s?App|California Court of Appeal", re.I)),
    ("cal", "Cal. Supreme Court", re.compile(r"Cal\.\s?[45]th|Supreme Court of California", re.I)),
    ("fcc", "FCC", re.compile(r"\bFCC\b")),
    ("cfpb", "CFPB", re.compile(r"\bCFPB\b")),
]

POSTURE_KEYWORDS: list[tuple[str, re.Pattern]] = [
    ("motion_to_dismiss", re.compile(r"motion to dismiss", re.I)),
    ("summary_judgment", re.compile(r"summary judgment", re.I)),
    ("class_certification", re.compile(r"class certification|certify(?:ing)? (?:a|the) class", re.I)),
    ("compel_arbitration", re.compile(r"compel arbitration", re.I)),
    ("motion_to_strike", re.compile(r"motion to strike", re.I)),
    ("demurrer", re.compile(r"demurrer", re.I)),
    ("appeal", re.compile(r"\bappeal(?:s|ed|ing)?\b|on remand", re.I)),
    ("preliminary_injunction", re.compile(r"preliminary injunction", re.I)),
]

STATUTE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("fdcpa", re.compile(r"Fair Debt Collection|FDCPA|Rosenthal|1692|1788\.17", re.I)),
    ("tcpa", re.compile(r"Telephone Consumer Protection|TCPA|227\(b\)|§\s?227", re.I)),
    ("fcra", re.compile(r"Fair Credit Reporting|FCRA|1681|CCRAA|1785\.25", re.I)),
    ("privacy", re.compile(r"\bCIPA\b|632\.7|638\.51|CCPA|1798\.100|1798\.150|wiretap|pen register", re.I)),
    ("arbitration", re.compile(r"Federal Arbitration Act|arbitration agreement|delegation clause", re.I)),
    ("class", re.compile(r"Rule 23|23\(b\)\(3\)|ascertainability|\bCAFA\b", re.I)),
    ("ucl", re.compile(r"\b17200\b|unfair competition law|\b17500\b|\bCLRA\b|\b1770\b", re.I)),
]

JUDGE_RE = re.compile(r"Judge\s+([A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+){0,2})")
WL_CITE_RE = re.compile(r"\b(19|20)\d{2}\s+WL\s+\d+\b")
DOCKET_RE = re.compile(r"No\.?\s*([A-Za-z0-9:\-]{4,40})")

# A "closing citation" line: case name, docket no., WL cite, pincite, court+date
# parenthetical — e.g. "Doe v. MKS Instruments, Inc., No. SACV2300868CJCKESX,
# 2023 WL 9421115, at *3 (C.D. Cal. Nov. 3, 2023)." Anchored on the WL-cite +
# trailing parenthetical shape so it doesn't match a WL cite merely mentioned
# inside a quoted passage earlier in the post.
CLOSING_CITE_RE = re.compile(
    r"[A-Z][A-Za-z.,'&\-\s]{0,120}?(?:\bv\.?\s[A-Za-z.,'&\-\s]{2,80}?|\bIn re\s[A-Za-z.,'&\-\s]{2,80}?)\*?,?\s*"
    r"(?:et al\.?\*?,?\s*)?(?:No\.?\s*[\w:\-]{3,40},?\s*)?"
    r"(19|20)\d{2}\s+WL\s+\d+,?\s*"
    r"(?:at\s+\*\d+(?:-{1,2}\d+)?,?\s*)?"
    r"\([^)]{4,60}\)\.?",
    re.I,
)


def normalize_ws(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    s = s.replace("–", "-").replace("—", "-")
    return s


def load_rows(xlsx_path: Path) -> list[dict]:
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb["old blog posts"]
    rows = list(ws.iter_rows(values_only=True))
    header = rows[0]
    out = []
    for r in rows[1:]:
        rec = dict(zip(header, r))
        out.append(rec)
    return out


def extract_intro(content: str) -> str:
    paras = [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]
    if not paras:
        return ""
    first = normalize_ws(paras[0])
    if len(first) <= 500:
        return first
    # fall back to first 1-3 sentences
    sentences = re.split(r"(?<=[.!?])\s+", first)
    out, total = [], 0
    for s in sentences[:3]:
        out.append(s)
        total += len(s)
        if total > 350:
            break
    return " ".join(out)


def detect_court(*texts: str | None) -> tuple[str | None, str | None]:
    """Try each text in priority order (intro, then closing citation, then
    full content as a last resort) and return the first court match. Scanning
    the whole document first was wrong: it frequently matched a court named
    inside a quoted *other* case rather than the court that decided the case
    Scott is actually writing about. The intro sentence and the closing
    citation both name the deciding court directly, so they're authoritative;
    full-content is a noisy fallback only used when both are silent."""
    for text in texts:
        if not text:
            continue
        for court_id, label, pat in COURT_PATTERNS:
            if pat.search(text):
                return court_id, label
    return None, None


def detect_postures(content: str) -> list[str]:
    return [key for key, pat in POSTURE_KEYWORDS if pat.search(content)]


def detect_statutes(content: str, categories: str) -> list[str]:
    hay = f"{content}\n{categories or ''}"
    return [key for key, pat in STATUTE_PATTERNS if pat.search(hay)]


def extract_judges(content: str) -> list[str]:
    return list({m.group(1).strip() for m in JUDGE_RE.finditer(content)})


def extract_closing_citation(content: str) -> str | None:
    """Find the LAST full citation-shaped match anywhere in the post — not
    just "the last paragraph", since a quoted passage earlier in the post can
    itself contain a WL cite (a citation to some OTHER case the court
    discussed) that would otherwise be mistaken for Scott's own closing cite."""
    stripped = content.rstrip()
    matches = list(CLOSING_CITE_RE.finditer(content))
    if not matches:
        return None
    m = matches[-1]
    # the true closing cite is the literal last thing in the post — allow a
    # little trailing whitespace/stray punctuation but nothing more. A match
    # merely somewhere in the back half is too loose: string-cites ("See
    # also X, No. ..., WL ... (Court Date)") inside narrative discussion
    # match the same shape without being the post's own closing citation.
    if len(stripped) - m.end() > 15:
        return None
    return normalize_ws(m.group(0).strip())


def title_from_url(url: str | None) -> str | None:
    if not url:
        return None
    slug = url.rstrip("/").split("/")[-1]
    return slug.replace("-", " ")


TITLE_VERB_RE = re.compile(
    r"\b(discusses|holds|finds|grants|denies|allows|rules|addresses|strikes|dismisses|"
    r"affirms|reverses|remands|clarifies|expands|explains)\b",
    re.I,
)


def summary_is_excerpt(content: str, summary: str) -> bool:
    c = re.sub(r"\s+", " ", (content or "")).replace("*", "").strip()
    s = re.sub(r"\s+", " ", (summary or "")).replace("*", "").strip().rstrip(".")
    if not s:
        return False
    return s[:200] in c[:600]


def build(xlsx_path: Path, author_filter: str | None) -> None:
    raw_rows = load_rows(xlsx_path)
    print(f"loaded {len(raw_rows)} rows from {xlsx_path}")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE posts (
            id INTEGER PRIMARY KEY,
            date TEXT, author TEXT, url TEXT, categories TEXT,
            content TEXT, summary TEXT, summary_is_excerpt INTEGER,
            intro TEXT, court_id TEXT, court_label TEXT,
            postures TEXT, statutes TEXT, judges TEXT,
            closing_citation TEXT, title_slug TEXT, word_count INTEGER,
            quote_share REAL
        )"""
    )

    category_counter: Counter[str] = Counter()
    title_verb_counter: Counter[str] = Counter()
    judge_counter: Counter[str] = Counter()
    intro_by_posture_court: dict[str, list[str]] = defaultdict(list)
    citation_examples_by_court: dict[str, list[str]] = defaultdict(list)
    quote_shares: list[float] = []
    n_scott = 0
    n_excerpt_summary = 0
    n_total = 0

    for rec in raw_rows:
        author = str(rec.get("Author") or "").strip()
        if author_filter and author != author_filter:
            continue
        n_total += 1
        if author == "Scott J. Hyman":
            n_scott += 1

        content = normalize_ws(str(rec.get("Article Content") or ""))
        summary = str(rec.get("Article Summary") or "")
        categories_raw = str(rec.get("Categories") or "")
        url = rec.get("Article URL")
        date = rec.get("Published Date")

        if not content:
            continue

        is_excerpt = summary_is_excerpt(content, summary)
        if is_excerpt:
            n_excerpt_summary += 1

        for cat in categories_raw.split(","):
            cat = cat.strip()
            if cat:
                category_counter[cat] += 1

        intro = extract_intro(content)
        closing = extract_closing_citation(content)
        court_id, court_label = detect_court(intro, closing, content)
        postures = detect_postures(content)
        statutes = detect_statutes(content, categories_raw)
        judges = extract_judges(content)
        for j in judges:
            judge_counter[j] += 1
        slug = title_from_url(str(url) if url else None)
        if slug:
            vm = TITLE_VERB_RE.search(slug)
            if vm:
                title_verb_counter[vm.group(1).lower()] += 1

        word_count = len(content.split())
        paras = [p for p in re.split(r"\n\s*\n", content) if p.strip()]
        quote_share = 1 - (len(intro) / max(len(content), 1))
        quote_shares.append(quote_share)

        key = f"{postures[0] if postures else 'none'}|{court_id or 'unknown'}"
        if len(intro_by_posture_court[key]) < 5:
            intro_by_posture_court[key].append(intro)
        if court_id and closing and len(citation_examples_by_court[court_id]) < 5:
            citation_examples_by_court[court_id].append(closing)

        conn.execute(
            """INSERT INTO posts (date, author, url, categories, content, summary,
                summary_is_excerpt, intro, court_id, court_label, postures,
                statutes, judges, closing_citation, title_slug, word_count, quote_share)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(date) if date else None, author, str(url) if url else None,
                categories_raw, content, summary, int(is_excerpt), intro,
                court_id, court_label, json.dumps(postures), json.dumps(statutes),
                json.dumps(judges), closing, slug, word_count, quote_share,
            ),
        )

    conn.commit()
    conn.close()

    frozen_categories = {c: n for c, n in category_counter.most_common() if n >= 5}
    top_judges = {j: n for j, n in judge_counter.most_common(60)}

    stats = {
        "n_total_filtered": n_total,
        "n_scott_posts": n_scott,
        "pct_summary_is_excerpt": round(100 * n_excerpt_summary / max(n_total, 1), 1),
        "category_frequency_all": dict(category_counter.most_common()),
        "frozen_category_vocabulary": frozen_categories,
        "frozen_category_count": len(frozen_categories),
        "title_verb_frequency": dict(title_verb_counter.most_common()),
        "historical_judges": top_judges,
        "intro_examples_by_posture_court": dict(intro_by_posture_court),
        "closing_citation_examples_by_court": dict(citation_examples_by_court),
        "quote_share_median": sorted(quote_shares)[len(quote_shares) // 2] if quote_shares else None,
    }
    STATS_PATH.write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    print(f"wrote {DB_PATH} ({n_total} rows, {n_scott} by Scott)")
    print(f"wrote {STATS_PATH}")
    print(f"frozen category vocabulary: {len(frozen_categories)} categories (>=5 uses)")
    print(f"summary-is-excerpt: {stats['pct_summary_is_excerpt']}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    ap.add_argument("--author", default=None, help="filter to one author, e.g. 'Scott J. Hyman'")
    args = ap.parse_args()
    build(args.xlsx, args.author)
