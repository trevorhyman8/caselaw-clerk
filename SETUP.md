# SETUP.md — provisioning runbook

This is a numbered runbook for standing up `caselaw-clerk` end to end. Every
step is tagged:
- **[AGENT]** — your AI assistant runs this (clone, install, run a verify
  script, set an env var you've pasted).
- **[HUMAN: SCOTT]** — only you can do this: create an account, pay for a
  service, paste a secret when the setup script prompts for it.

Each phase ends with a **gate**: don't move to the next phase until the
listed `verify/vN_*.py` script prints `PASS`. If it doesn't, stop and report
exactly what failed — don't try to force it through.

---

## Phase 0 — Corpus + style foundation (already done for the pilot)

Trevor prepared a private bundle (`STYLE.md`, `corpus/corpus.sqlite`, the
underlying `.private/corpus.xlsx`) from your old Severson & Werson blog
archive — it's how the system learned to write in your voice. This bundle
is **not** in the public GitHub repo (on purpose — it's your content, not
generic code) and ships to you separately.

- **[HUMAN: SCOTT]** Get the private bundle from Trevor (AirDrop, encrypted
  zip, however you two exchange files) and place it at the repo root —
  `STYLE.md` goes in the root; `corpus.sqlite` goes in `corpus/`.
- **[AGENT]** `uv sync` to install dependencies.
- **Gate:** `uv run python verify/v0_corpus.py` → PASS (confirms STYLE.md
  and the exemplar index are present and readable).

## Phase 1 — Ingestion (CourtListener account)

- **[HUMAN: SCOTT]** Create a free account at https://www.courtlistener.com,
  then generate an API token at
  https://www.courtlistener.com/help/api/rest/v4/authentication/. Paste it
  when prompted.
- **[HUMAN: SCOTT]** Decide on the Free Law Project membership
  (~$10/month, https://free.law/membership/). This unlocks RECAP search
  alerts — the mechanism that catches *unpublished* federal district-court
  orders, which is most of what you used to write about. The backtest run
  during dev found only ~20% historical coverage *without* a token — see
  `verify/v1_results.json` for the honest numbers and why (mostly: RECAP
  needs auth to search well). Recommended: get the membership.
- **[AGENT]** Set `COURTLISTENER_TOKEN` (Railway variable or local `.env`).
  Create the 5 saved search alerts from `config/queries.yaml`'s
  `courtlistener_alerts` list on courtlistener.com, pointed at the
  receiver's webhook URL (`https://<your-railway-domain>/webhooks/courtlistener/<CL_WEBHOOK_SECRET>`
  — generate a random `CL_WEBHOOK_SECRET` value yourself and set it as an
  env var too).
- **[AGENT]** Re-run the coverage backtest WITH the token:
  `uv run python -m verify.v1_backtest_coverage --n 30` and report the new
  percentage — this is the number that should actually govern whether
  automated ingestion is trustworthy enough to rely on, versus leaning more
  on the manual-forward path (Phase 1b).
- **Gate:** coverage ≥ 40% (with token). Below that, Phase 1b becomes the
  primary path, not a backup.

### Phase 1b — Manual-forward path (works regardless of Phase 1's outcome)

You can always text the bot a case name, docket number, or a forwarded
Westlaw alert email, and it'll ingest that case directly — this doesn't
depend on CourtListener at all. **If you have Westlaw/WestClip access at
the firm, forwarding your existing alerts is likely the highest-coverage
option and worth setting up regardless of the automated-ingestion number
above.**

## Phase 2 — WordPress connection (do this LAST — Phase 4 gates it)

Skip ahead to Phase 3 first. Come back here only after you've reviewed the
system's output (Phase 4) and are comfortable connecting it to the live
blog.

- **[HUMAN: SCOTT]** Create a developer app at
  https://developer.wordpress.com/apps/ (any name, e.g. "Caselaw Clerk") →
  copy the Client ID and Client Secret.
- **[HUMAN: SCOTT]** Create an account-level Application Password at
  https://wordpress.com/me/security (this works alongside 2FA — it's a
  separate credential, not your login password).
- **[AGENT]** Set `WPCOM_CLIENT_ID`, `WPCOM_CLIENT_SECRET`, then run the
  token exchange helper (prompts for your WordPress.com username + the
  application password, exchanges them for a long-lived bearer token, and
  writes `WPCOM_ACCESS_TOKEN` for you — the application password itself is
  never stored). Resolve `WPCOM_SITE_ID` for
  `consumerfinanceprivacycounsel.com`.
- **Gate:** `uv run python verify/v4_wordpress.py` → PASS. This creates a
  test draft on the LIVE site titled "TEST — DELETE ME", reads it back to
  confirm it landed correctly, then deletes it. Nothing publishable exists
  on the site at any point.
- Publishing stays in `draft_only` mode (`config/config.yaml`) — the system
  will never flip a post to live/public until you explicitly change that
  AND separately confirm each publish in the Telegram chat. Both gates have
  to be true, not either one.

## Phase 3 — Telegram bot + Railway deployment

- **[HUMAN: SCOTT]** Open Telegram, message **@BotFather**, run `/newbot`,
  follow the prompts (pick any name/username) — you'll get a bot token back
  immediately. This is free and takes about 2 minutes.
- **[HUMAN: SCOTT]** Message your new bot once (anything) so Telegram knows
  you exist to it, then get your own numeric Telegram user ID — the
  easiest way is messaging **@userinfobot**, which replies with it
  instantly.
- **[HUMAN: SCOTT]** Create a Railway account at https://railway.app
  (GitHub login is fine) and a payment method — Railway's free tier doesn't
  cover an always-on service. Budget roughly **$25-40/month total**
  (Railway hosting + the optional Free Law Project membership + Anthropic
  API usage for drafting — see the cost breakdown in the 1-pager Trevor
  gave you, or ask your AI to re-derive it from `config/config.yaml` and
  current Anthropic pricing).
- **[HUMAN: SCOTT]** Create an Anthropic API key at
  https://console.anthropic.com (this is what actually pays for each
  drafted post, roughly $0.20-0.65 per post — see `pipeline/llm.py`'s
  docstring for why this is separate from a Claude subscription).
- **[AGENT]** `railway init`, create the `receiver` and `cron-runner`
  services from this repo (see `railway.json`), add a Postgres plugin, set
  `DATABASE_URL` from Railway's auto-generated value, and set every other
  env var from `.env.example` using `railway variables set`. Set
  `LLM_BACKEND=anthropic_api` (the deployed service can't use `claude -p`
  — see `pipeline/llm.py`). Deploy.
- **Gate:** `curl https://<railway-domain>/healthz` returns `{"status":
  "ok", ...}`. Then run `uv run python verify/v5_telegram.py` (sends a test
  digest to your own Telegram ID via the deployed bot) and confirm you
  receive it and can reply.

## Phase 4 — Shadow week + your approval

- **[AGENT]** Leave `PIPELINE_MODE=shadow` for at least 7 days. Every
  morning, compare the digest against what you'd actually have blogged
  about that week.
- **[AGENT]** After the shadow week, package: `HALLUCINATION-CONTROLS.md`,
  3-5 real drafts with their report cards, and the demonstration that the
  verification gate catches deliberately-corrupted drafts (see
  `verify/v6_gate_calibration.py`).
- **[HUMAN: SCOTT]** Review that package. This is the actual approval
  gate for Phase 2 (WordPress) and for flipping `publishing_mode` to
  `live` — nothing publishes to your real blog until you're satisfied.

## Phase 5 — Handoff / ongoing

- **[HUMAN: SCOTT]** Once you're comfortable, this whole system is yours —
  your Railway account, your Anthropic key, your CourtListener account. You
  or your AI can adjust anything in `config/config.yaml` without touching
  code (digest timing, how many posts get pre-drafted overnight, which
  courts are watched).
- If something breaks, ask your AI to read `CLAUDE.md` first — it has the
  ground rules for working in this repo safely.

---

## Known gaps (see `pipeline/ingest/GAPS.md` for full detail)

- FCC's RSS feeds are blocked by their own server for automated access —
  not wired yet.
- GovInfo weekly reconciliation (a second-opinion check on what the primary
  ingestion missed) is designed but not built — needs a free GovInfo API
  key from https://api.data.gov/signup/.
- CA Legislature bill tracking is manual (you flag a bill, the system
  doesn't discover them on its own).
- Occasional PDF text-extraction imperfections at page breaks can cause a
  real quote to fail verification and route to manual review rather than
  auto-approve — this is the safety design working as intended, not a bug
  to "fix" by loosening the quote checker.
