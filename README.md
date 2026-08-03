# caselaw-clerk

Drafts case-note blog posts for [consumerfinanceprivacycounsel.com](https://consumerfinanceprivacycounsel.com/)
from real court decisions, in Scott Hyman's own voice (learned from 2,939 of
his past posts), gated behind a 4-layer anti-hallucination verification
pipeline, delivered and approved through a Telegram bot.

Every draft is scoped to fetched, hashed, official source text — the
opinion text and citation metadata — never to the model's memory of the
law. See `HALLUCINATION-CONTROLS.md` for how that's enforced and verified.

**Start here:** `SETUP.md` is a numbered runbook for provisioning this from
scratch. `CLAUDE.md` has ground rules for any AI working in this repo.

## Layout

- `corpus/` — one-time corpus distillation from the historical blog archive
  (the private bundle — xlsx/sqlite/STYLE.md — is gitignored, ships
  separately)
- `pipeline/ingest/` — CourtListener + California Courts + CFPB fetchers,
  the evidence locker (dedupe, storage)
- `pipeline/style/` — exemplar retrieval for few-shot drafting
- `pipeline/verify_gate/` — the 4-layer verification gate
- `pipeline/draft/` — the content agent
- `pipeline/notify/` — Telegram state machine + intent parsing
- `pipeline/publish/` — WordPress.com publisher
- `pipeline/jobs/` — the cron-runner entrypoints (sweep, digest, recap)
- `pipeline/receiver/` — the FastAPI webhook receiver
- `config/` — every operational choice lives here, nowhere else
- `verify/` — the gate scripts SETUP.md's phases check against
