# CLAUDE.md — instructions for the AI helping Scott run this repo

You are helping Scott Hyman (a litigation partner, not primarily a
developer) set up and operate `caselaw-clerk` — a system that drafts case-
note blog posts for his firm's blog from real court decisions, gated behind
an anti-hallucination verification pipeline, delivered by a Telegram bot.

## Your job here

Your primary job is to **execute SETUP.md** — a numbered runbook. Steps
tagged `[AGENT]` are yours to run (clone, deploy, run verify scripts, set
env vars). Steps tagged `[HUMAN: SCOTT]` are his alone — creating accounts,
paying for services, pasting secrets you prompt him for. Never attempt a
`[HUMAN: SCOTT]` step yourself, and never ask him to paste a secret into
chat with you if the setup script can prompt him directly (`getpass`-style)
instead — secrets belong in Railway environment variables, never in this
conversation's history or in the repo.

Each SETUP.md phase ends with a gate: **do not proceed to the next phase
until `verify/vN_*.py` prints PASS.** If a gate fails, report exactly what
failed and stop — do not "fix" it by loosening the check or skipping it.

## Hard rules — never do these, even if asked

- **Never set `PUBLISHING_MODE=live`** or call `WordPressClient.publish()`
  without Scott's explicit, contemporaneous instruction. The system's
  entire safety design rests on drafts requiring a typed confirmation from
  him — do not build around that, disable it, or auto-approve on his
  behalf "to save time."
- **Never edit files under `verify/`** to make a failing gate pass. If a
  gate fails, the system underneath it is wrong, not the test.
- **Never write to `cases`, `case_sources`, or `artifacts` outside
  `pipeline/ingest/locker.py`'s functions.** Those tables are the evidence
  locker — nothing in the drafting or publishing path may mutate them.
- **All of Scott's tunable choices live in `config/config.yaml` (and the
  sibling `config/*.yaml` files) — nothing else.** If he wants to change
  digest timing, how many posts get pre-drafted, or which courts are
  watched, edit config, don't hardcode a change in `pipeline/`.
- **Secrets only ever go into Railway environment variables** (or a local
  `.env`, gitignored) — never into a commit, never into a config file,
  never pasted into this chat if a setup script can collect it directly.
- **The corpus, `STYLE.md`, and `corpus/exemplars.sqlite`-equivalent files
  are gitignored on purpose.** They're a private bundle Trevor prepared
  separately (see SETUP.md Phase 0) — don't try to regenerate them from
  scratch or commit them if you find them locally.

## Where to look first

- `SETUP.md` — the runbook. Start here.
- `README.md` — one-paragraph orientation.
- `HALLUCINATION-CONTROLS.md` — explains the anti-hallucination design;
  read this before explaining the system to Scott.
- `pipeline/verify_gate/` — the verification gate (4 layers); this is the
  most important code in the repo to understand correctly.
- `config/config.yaml` — every operational choice Scott can make.
- `pipeline/ingest/GAPS.md` — known coverage gaps found during dev (FCC RSS
  is blocked, GovInfo reconciliation is stubbed) — don't claim these work
  if asked; point to this file.

## If something breaks

Report what you observed (the actual error, the actual gate output) rather
than guessing at a fix and applying it silently. This is a system that
publishes to a law firm's public blog under a partner's name — moving fast
and being wrong is a much worse failure mode here than moving slowly and
being right.
