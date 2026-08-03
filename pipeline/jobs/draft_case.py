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


MAX_AUTO_REPAIRS = 2  # up to 3 total attempts (1 original + 2 repairs) before giving up


def _build_repair_instruction(gate_result, fact_sheet, case_meta: dict) -> str:
    """Turns a failed gate result into a SPECIFIC instruction for the next
    draft attempt — not "try again", but exactly what was wrong and what to
    do about it. This is what makes the retry loop actually converge
    instead of repeating the same mistake."""
    lines = ["Your previous draft failed verification. Fix the SPECIFIC problems below — do not just rewrite generally:"]

    failed_quotes = [
        s for s in gate_result.provenance.get("quote_map", []) if s.get("tier") == "fail"
    ]
    for s in failed_quotes:
        lines.append(
            f'- This quoted passage could NOT be found verbatim in the source opinion: '
            f'"{s["text"]}". Either quote it EXACTLY as it appears in the opinion text you were '
            f'given (character for character), or replace it with a different real passage that '
            f'actually supports the same point.'
        )

    closing = gate_result.provenance.get("citations", {}).get("closing", [])
    for check in closing:
        if not check.get("passed"):
            lines.append(
                f'- The closing citation\'s "{check["field"]}" field did not match the case record '
                f'(expected: {case_meta.get(check["field"] if check["field"] != "party_names" else "case_name")!r}). '
                f'Fix the closing citation to match exactly.'
            )

    other = gate_result.provenance.get("citations", {}).get("other", [])
    for c in other:
        if not c.get("passed"):
            lines.append(
                f'- This citation in your intro/commentary could not be verified: "{c["raw"]}". '
                f'Remove it unless it appears verbatim in the source opinion.'
            )

    fact_fails = [
        a["name"] for a in gate_result.provenance.get("fact_assertions", []) if not a.get("passed")
    ]
    if "holding_direction" in fact_fails:
        lines.append(
            f"- Your intro stated the wrong outcome. The court's actual holding direction is: "
            f"'{fact_sheet.holding_direction}'. Rewrite the intro sentence to correctly state this "
            f"— this is the most important fix; a wrong outcome is the worst possible error here."
        )
    if "posture" in fact_fails:
        lines.append(f"- Your intro didn't clearly name the procedural posture: '{fact_sheet.posture}'. State it explicitly.")
    if "judge" in fact_fails:
        lines.append(f"- Your intro should name the deciding judge: {case_meta.get('judge')}.")

    violations = gate_result.provenance.get("adversarial", {}).get("violations", [])
    for v in violations:
        lines.append(f'- Unsupported claim: "{v["draft_text"]}" — {v["why"]}. Remove or fix this.')

    return "\n".join(lines)


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

    cl_client = CourtListenerClient() if settings.courtlistener_token else None
    try:
        draft, gate_result, attempts = None, None, 0
        current_angle = angle
        for attempt in range(MAX_AUTO_REPAIRS + 1):
            attempts = attempt + 1
            draft = draft_post(case_meta, opinion_text, fact_sheet, exemplars, frozen_categories, artifact["sha256"], current_angle)
            gate_result = run_gate(draft, case_meta, opinion_text, fact_sheet, artifact["sha256"], cl_client)
            if gate_result.verdict in ("pass", "warn"):
                break
            if attempt < MAX_AUTO_REPAIRS:
                repair = _build_repair_instruction(gate_result, fact_sheet, case_meta)
                current_angle = f"{angle}\n\n{repair}" if angle else repair
    finally:
        if cl_client:
            cl_client.close()

    # Always store and return whatever the LAST attempt produced — a
    # needs_review draft is still real content Scott can read, edit, or
    # decide is fine; it just can't be published without a fix (see
    # run_publish's hard gate_status check). Silence was the actual bug
    # here, not the verification failing.
    draft_id = _store_draft(case_id, draft, gate_result, artifact["sha256"])
    report_card = render_report_card(gate_result, case["case_name"], case["court_id"], artifact["source_url"])
    if attempts > 1:
        report_card = f"({attempts} attempts — auto-repair {'succeeded' if gate_result.verdict != 'needs_review' else 'did not fully resolve every issue'})\n\n" + report_card

    return {
        "draft_id": draft_id,
        "content_hash": gate_result.provenance["provenance_sha256"],
        "gate_verdict": gate_result.verdict,
        "reasons": gate_result.reasons,
        "report_card": report_card,
        "attempts": attempts,
        # The FULL post text, not just the report card — sent directly in
        # Telegram since no web preview host is deployed yet (see SETUP.md).
        # A future real preview link can be layered on top of this later;
        # it isn't a substitute for showing the actual content in-chat.
        "draft_text": _render_draft_text(draft),
    }


def _render_draft_text(draft: dict) -> str:
    parts = [draft.get("intro", "")]
    for block in draft.get("blocks", []):
        prefix = "> " if block.get("type") == "quote" else ""
        parts.append(prefix + block.get("text", ""))
    parts.append(draft.get("closing_citation") or "")
    return "\n\n".join(p for p in parts if p)


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
