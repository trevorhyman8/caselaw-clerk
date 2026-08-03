"""FastAPI webhook receiver — the always-on Railway service. Two webhook
paths (CourtListener search alerts, Telegram updates) plus a health check.
The cron-runner (pipeline/jobs/run_due.py) is a SEPARATE Railway service on
the same image (see railway.json) — this process only handles inbound
pushes, it does not poll or sweep itself.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request

from pipeline.db.db import init_schema
from pipeline.settings import settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("receiver")

app = FastAPI(title="caselaw-clerk receiver")


@app.on_event("startup")
async def startup() -> None:
    init_schema()


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "pipeline_mode": settings.pipeline_mode}


@app.post("/webhooks/courtlistener/{secret}")
async def courtlistener_webhook(secret: str, request: Request) -> dict:
    if not settings.cl_webhook_secret or secret != settings.cl_webhook_secret:
        raise HTTPException(status_code=403, detail="bad webhook secret")
    payload = await request.json()
    # Idempotency key from CourtListener's own delivery headers, per their
    # webhook docs — dedupe on it before processing (not yet wired to a
    # dedupe table; alerts require a live CourtListener account with a
    # created search alert, which is a backloaded operator step — this
    # handler is structurally ready for when that exists).
    log.info(f"courtlistener webhook received: {str(payload)[:200]}")
    from pipeline.ingest.webhook_handler import handle_courtlistener_alert

    handle_courtlistener_alert(payload)
    return {"ok": True}


@app.post("/webhooks/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request) -> dict:
    if not settings.telegram_bot_token or secret != settings.telegram_bot_token[:16]:
        raise HTTPException(status_code=403, detail="bad webhook secret")
    payload = await request.json()
    from telegram import Update

    from pipeline.notify.telegram_bot import build_app

    application = build_app()
    update = Update.de_json(payload, application.bot)
    await application.process_update(update)
    return {"ok": True}
