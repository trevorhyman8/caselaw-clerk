"""Shared draft pipeline: case_id -> fact sheet -> exemplars -> draft ->
verification gate -> stored provenance + report card. Used by both the
interactive Telegram path (notify/telegram_bot.py) and the overnight
Batch pre-drafting job (jobs/morning_digest.py) — one implementation, so
"reply 1" and "pre-drafted, tap to view" always produce identical results
for the same case.
"""
from __future__ import annotations

import json
import sqlite3

from pipeline.db import db
from pipeline.draft.content_agent import draft_post
from pipeline.ingest.courtlistener import CourtListenerClient
from pipeline.settings import ROOT, settings
from pipeline.style.exemplars import retrieve
from pipeline.verify_gate.facts import extract_fact_sheet
from pipeline.verify_gate.gate import render_report_card, run_gate


def _load_case_and_artifact(case_id: str) -> tuple[dict, dict]:
    with db.get_conn() as conn:
        conn.row_factory = sqlite3.Row
        case = conn.execute(db.qmark("SELECT * FROM cases WHERE id = ?"), (case_id,)).fetchone()
        if not case:
            raise ValueError(f"unknown case_id {case_id}")
        artifact = conn.execute(
            db.qmark(
                "SELECT * FROM artifacts WHERE case_id = ? AND kind = 'opinion_text' "
                "ORDER BY fetched_at DESC LIMIT 1"
            ),
            (case_id,),
        ).fetchone()
        if not artifact:
            raise ValueError(f"no opinion_text artifact for case_id {case_id}")
        return dict(case), dict(artifact)


def _frozen_categories() -> list[str]:
    stats_path = ROOT / "corpus" / "style_stats.json"
    if not stats_path.exists():
        return []
    return list(json.loads(stats_path.read_text())["frozen_category_vocabulary"].keys())


def _store_draft(case_id: str, draft: dict, gate_result, artifact_sha256: str) -> str:
    draft_id = db.new_id()
    body_rendered = draft.get("intro", "") + "\n\n" + "\n\n".join(
        b.get("text", "") for b in draft.get("blocks", [])
    ) + "\n\n" + (draft.get("closing_citation") or "")
    content_hash = db.new_id()  # distinct from provenance hash; identifies THIS draft revision
    with db.get_conn() as conn:
        conn.execute(
            db.qmark(
                "INSERT INTO drafts (id, case_id, revision, title, slug, intro, blocks, "
                "closing_citation, categories, excerpt, body_rendered, artifact_shas, "
                "gate_status, provenance, created_at) VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)"
            ),
            (
                draft_id, case_id, draft.get("title"), draft.get("slug"), draft.get("intro"),
                db.dumps(draft.get("blocks", [])), draft.get("closing_citation"),
                db.dumps(draft.get("categories", [])), body_rendered[:400], body_rendered,
                db.dumps([artifact_sha256]), gate_result.verdict, db.dumps(gate_result.provenance),
                db.now_iso(),
            ),
        )
        conn.execute(
            db.qmark("UPDATE cases SET status = ?, updated_at = ? WHERE id = ?"),
            (f"drafted_{gate_result.verdict}", db.now_iso(), case_id),
        )
    return draft_id


def run_draft_pipeline(case_id: str, angle: str | None = None) -> dict:
    case, artifact = _load_case_and_artifact(case_id)
    opinion_text = artifact["content_text"]

    case_meta = {
        "case_name": case["case_name"], "court_id": case["court_id"],
        "docket_number": case["docket_number"], "date_filed": case["date_filed"],
        "judge": case["judge"],
    }

    fact_sheet = extract_fact_sheet(opinion_text, case_meta)
    exemplars = retrieve(statutes=fact_sheet.statutes, court_id=case["court_id"], posture=fact_sheet.posture, limit=3)
    frozen_categories = _frozen_categories()

    draft = draft_post(case_meta, opinion_text, fact_sheet, exemplars, frozen_categories, artifact["sha256"], angle)

    cl_client = CourtListenerClient() if settings.courtlistener_token else None
    try:
        gate_result = run_gate(draft, case_meta, opinion_text, fact_sheet, artifact["sha256"], cl_client)
    finally:
        if cl_client:
            cl_client.close()

    draft_id = _store_draft(case_id, draft, gate_result, artifact["sha256"])
    report_card = render_report_card(gate_result, case["case_name"], case["court_id"], artifact["source_url"])

    return {
        "draft_id": draft_id,
        "content_hash": gate_result.provenance["provenance_sha256"],
        "gate_verdict": gate_result.verdict,
        "reasons": gate_result.reasons,
        "report_card": report_card,
        "preview_url": f"https://preview.example/d/{draft_id}",  # real preview host TBD, see SETUP.md
    }


def run_revision_pipeline(draft_id: str, edit_instruction: str) -> dict:
    """Full redraft with the edit instruction folded into the angle field
    and the previous draft as an exemplar-of-one — then the FULL gate
    re-runs unconditionally (plan: "full gate re-runs unconditionally" on
    every edit round, never a partial re-check)."""
    with db.get_conn() as conn:
        conn.row_factory = sqlite3.Row
        prev = conn.execute(db.qmark("SELECT * FROM drafts WHERE id = ?"), (draft_id,)).fetchone()
        if not prev:
            raise ValueError(f"unknown draft_id {draft_id}")
    angle = f"Revise per this instruction from Scott: \"{edit_instruction}\". Previous draft title was: {prev['title']}"
    return run_draft_pipeline(prev["case_id"], angle=angle)


def run_publish(draft_id: str, expected_content_hash: str | None) -> dict:
    from pipeline.publish.wordpress import WordPressClient

    with db.get_conn() as conn:
        conn.row_factory = sqlite3.Row
        draft = conn.execute(db.qmark("SELECT * FROM drafts WHERE id = ?"), (draft_id,)).fetchone()
    if not draft:
        return {"message": "Couldn't find that draft."}

    provenance = db.loads(draft["provenance"])
    if provenance.get("provenance_sha256") != expected_content_hash:
        return {"message": "This draft changed since you last saw it — reply 'publish' again on the latest preview."}
    if draft["gate_status"] not in ("pass", "warn"):
        return {"message": "This draft never cleared verification and cannot be published."}

    if settings.publishing_mode != "live":
        return {"message": f"[shadow mode] Would publish '{draft['title']}' now — publishing_mode is not 'live' yet."}

    client = WordPressClient()
    try:
        categories = db.loads(draft["categories"]) or []
        cat_ids = client.resolve_category_ids(categories)
        post = client.create_draft(draft["title"], draft["body_rendered"], draft["excerpt"] or "", cat_ids)
        published = client.publish(post["id"])
        return {"message": f"✅ Published: {published.get('link', '(no link returned)')}"}
    finally:
        client.close()
