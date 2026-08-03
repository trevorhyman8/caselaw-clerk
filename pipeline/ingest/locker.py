"""Evidence-locker writer: dedupe ladder + INSERT-only access to
cases/case_sources/artifacts. This is the ONLY module allowed to write those
three tables. The drafting stage (pipeline/draft/) may only SELECT from them.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from pipeline.db import db

DOCKET_STRIP_RE = re.compile(r"[^A-Za-z0-9]")
NAME_TOKEN_RE = re.compile(r"[A-Za-z]{3,}")


def normalize_docket(docket: str | None) -> str | None:
    if not docket:
        return None
    return DOCKET_STRIP_RE.sub("", docket).upper()


def first_party_tokens(case_name: str) -> set[str]:
    lead = case_name.split(" v. ")[0].split(" v ")[0]
    return {t.upper() for t in NAME_TOKEN_RE.findall(lead)}


@dataclass
class NewCase:
    case_name: str
    court_id: str
    docket_number: str | None = None
    judge: str | None = None
    date_filed: str | None = None
    cl_cluster_id: int | None = None
    cl_docket_id: int | None = None
    source: str = "courtlistener_sweep"
    source_url: str = ""
    raw_payload: dict = field(default_factory=dict)


def _find_existing(conn, new: NewCase) -> str | None:
    """Dedupe ladder — see plan §2.4. Returns an existing case id or None."""
    # 1. CourtListener IDs
    if new.cl_cluster_id:
        row = conn.execute(
            db.qmark("SELECT id FROM cases WHERE cl_cluster_id = ?"), (new.cl_cluster_id,)
        ).fetchone()
        if row:
            return row[0]
    if new.cl_docket_id:
        row = conn.execute(
            db.qmark("SELECT id FROM cases WHERE cl_docket_id = ?"), (new.cl_docket_id,)
        ).fetchone()
        if row:
            return row[0]

    # 2. normalized docket + court
    norm = normalize_docket(new.docket_number)
    if norm:
        row = conn.execute(
            db.qmark("SELECT id FROM cases WHERE docket_number_norm = ? AND court_id = ?"),
            (norm, new.court_id),
        ).fetchone()
        if row:
            return row[0]

    # 3. (artifact-hash dedupe happens in add_artifact, not here — a case
    #    record must already exist to attach an artifact to)

    # 4. fuzzy caption: same court, date within +/-3 days, first-party token
    #    overlap >= 92. Logged to dedupe_review rather than auto-merged when
    #    it's the ONLY signal (no docket match) — a human should confirm.
    if new.date_filed:
        candidates = conn.execute(
            db.qmark(
                "SELECT id, case_name, date_filed FROM cases WHERE court_id = ? "
                "AND date_filed IS NOT NULL"
            ),
            (new.court_id,),
        ).fetchall()
        new_tokens = first_party_tokens(new.case_name)
        for row in candidates:
            cid, cname, dfiled = row[0], row[1], row[2]
            if not dfiled:
                continue
            try:
                delta_days = abs((_parse_date(dfiled) - _parse_date(new.date_filed)).days)
            except ValueError:
                continue
            if delta_days > 3:
                continue
            existing_tokens = first_party_tokens(cname)
            if not new_tokens or not existing_tokens:
                continue
            overlap = len(new_tokens & existing_tokens) / max(len(new_tokens), 1)
            score = fuzz.token_set_ratio(new.case_name, cname)
            if overlap >= 0.5 and score >= 92:
                _log_dedupe_review(conn, cid, cid, score, "fuzzy caption match on ingest")
                return cid
    return None


def _parse_date(s: str):
    from datetime import date

    return date.fromisoformat(s[:10])


def _log_dedupe_review(conn, case_id_a: str, case_id_b: str, similarity: float, reason: str) -> None:
    conn.execute(
        db.qmark(
            "INSERT INTO dedupe_review (id, case_id_a, case_id_b, similarity, reason, "
            "resolved, created_at) VALUES (?,?,?,?,?,0,?)"
        ),
        (db.new_id(), case_id_a, case_id_b, similarity, reason, db.now_iso()),
    )


def canonical_key(new: NewCase) -> str:
    if new.cl_cluster_id:
        return f"cl:{new.cl_cluster_id}"
    norm = normalize_docket(new.docket_number) or ""
    raw = f"{new.court_id}|{norm}|{new.date_filed or ''}|{sorted(first_party_tokens(new.case_name))}"
    return "sha1:" + hashlib.sha1(raw.encode()).hexdigest()


def upsert_case(new: NewCase) -> tuple[str, bool]:
    """Insert the case (if new) and always record the sighting in
    case_sources. Returns (case_id, was_new)."""
    with db.get_conn() as conn:
        existing_id = _find_existing(conn, new)
        if existing_id:
            case_id, was_new = existing_id, False
        else:
            case_id = db.new_id()
            was_new = True
            now = db.now_iso()
            conn.execute(
                db.qmark(
                    "INSERT INTO cases (id, canonical_key, case_name, court_id, "
                    "docket_number, docket_number_norm, judge, date_filed, "
                    "cl_cluster_id, cl_docket_id, status, first_seen_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                ),
                (
                    case_id, canonical_key(new), new.case_name, new.court_id,
                    new.docket_number, normalize_docket(new.docket_number), new.judge,
                    new.date_filed, new.cl_cluster_id, new.cl_docket_id, "captured",
                    now, now,
                ),
            )
        conn.execute(
            db.qmark(
                "INSERT INTO case_sources (id, case_id, source, source_url, "
                "raw_payload, fetched_at) VALUES (?,?,?,?,?,?)"
            ),
            (db.new_id(), case_id, new.source, new.source_url, db.dumps(new.raw_payload), db.now_iso()),
        )
        return case_id, was_new


def add_artifact(
    case_id: str, kind: str, content_bytes: bytes | None, content_text: str | None,
    source_url: str,
) -> tuple[str, bool]:
    """INSERT-only. Returns (artifact_id, was_new) — hash dedupe means a
    second fetch of the identical bytes is a no-op, not a duplicate row."""
    sha = hashlib.sha256(content_bytes or (content_text or "").encode()).hexdigest()
    size = len(content_bytes) if content_bytes else len(content_text or "")
    with db.get_conn() as conn:
        existing = conn.execute(
            db.qmark(
                "SELECT id FROM artifacts WHERE case_id = ? AND kind = ? AND sha256 = ?"
            ),
            (case_id, kind, sha),
        ).fetchone()
        if existing:
            return existing[0], False
        artifact_id = db.new_id()
        conn.execute(
            db.qmark(
                "INSERT INTO artifacts (id, case_id, kind, sha256, byte_size, "
                "content_text, content_bytes, source_url, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)"
            ),
            (
                artifact_id, case_id, kind, sha, size, content_text,
                content_bytes, source_url, db.now_iso(),
            ),
        )
        return artifact_id, True
