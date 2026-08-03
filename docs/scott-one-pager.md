# Caselaw Clerk: How It Works

*A plain-language guide for Scott Hyman — August 2026*

## The idea in one sentence

Every morning, before you get to the office, a system reads the new court
decisions in your practice areas, drafts a blog post about the most
interesting one in your writing style, checks its own work against the
actual court opinion, and texts you the result — you tap "publish" or tell
it what to change.

You said the biggest risk is the system making things up. This document
explains how it's built to prevent that, how it works day to day, and what
it costs — because a few of the pieces genuinely require you to pay for a
subscription, and you should know exactly what each dollar buys before you
commit to it.

---

## What "the system" actually is

Think of it as four separate jobs, each done by a different piece of
software, that hand off to one another automatically:

**1. A researcher.** Every day, it checks public legal databases — the
main one is called CourtListener, essentially a free, searchable archive of
federal and state court opinions — for new decisions matching the areas you
write about (FDCPA, TCPA, FCRA, CCPA/privacy, arbitration, class actions).
When it finds one, it downloads the court's own official document (a PDF),
saves it, and — this matters — computes a cryptographic fingerprint of that
exact document. That fingerprint is how the system proves, forever, exactly
which document any later draft was based on.

**2. An editor.** It scores every new case using the same rough priorities
your own writing history shows (FDCPA and TCPA cases score highest; that's
not a guess, it's counted directly from your ~2,900 old posts). The
highest-scoring 3-8 cases each morning become your digest.

**3. A writer.** When you pick a case to draft, it writes a post in your
voice — the intro-sentence patterns, the citation format, the habit of
quoting the court's own language rather than paraphrasing it, all learned
from your archive, not invented.

**4. A fact-checker that doesn't trust the writer.** This is the part built
specifically for your concern, and it's covered in detail below.

Your interface to all of this is a **Telegram** chat with a bot — you get a
message, you reply with a number or a word, that's it.

---

## Where hallucination risk actually lives, and what's done about each spot

You identified two places this could go wrong. Here's how each is handled,
concretely — not as a promise, but as something already built and tested
against real court opinions during development.

### Risk 1: Finding and storing the right document

The system never asks an AI "what did this case say" from memory. It
downloads the court's actual PDF from an official source, extracts the
text, and stores both the original file and its fingerprint. Every later
step — drafting, checking, publishing — is only ever allowed to read *that
stored document*, never to rely on an AI's general knowledge of the law.
That storage is also locked down so nothing downstream can quietly edit or
delete what was captured — it's an append-only record, like a case file
that only grows.

### Risk 2: Whether the drafted post is actually faithful to the opinion

Every draft goes through four separate, independent checks before you ever
see it:

1. **Every quoted sentence is checked character-for-character** against the
   stored opinion. Not "does this sound right" — an exact text match. During
   testing, deliberately changing one word in a real quote (flipping
   "reversed" to "affirmed" — the opposite legal outcome) was caught
   instantly.
2. **Every citation is cross-checked** against the case's actual court
   record (docket number, judge, date, party names) and, for any other case
   mentioned, against a lookup tool CourtListener built specifically to
   catch fabricated citations.
3. **The facts of the case — who the judge was, what was argued, which way
   the court ruled — are extracted separately, before the post is even
   written**, and every one of those facts is itself verified as a direct
   quote from the opinion. If a draft's opening sentence doesn't match that
   pre-verified holding, it's rejected outright — no partial credit, no
   patch, a full rewrite. Getting the winner/loser backwards is the single
   worst thing this system could do to your name, so that check has no
   shortcuts.
4. **A second, independent AI reviews the finished draft adversarially** —
   it's told nothing about the case except "find anything not directly
   supported by this document," with no stake in the draft looking good.
   During testing, this step caught a fabricated damages figure, a
   fabricated dissenting opinion, and — the one that actually surprised
   me — a real draft that correctly quoted the court's official case
   summary but wrongly attributed that language to the judge's own
   reasoning, when the opinion itself says the summary "constitutes no part
   of the opinion of the court."

If any of the four checks fails, the draft is flagged for your review
instead of being silently "cleaned up." **Nothing publishes automatically —
ever.** Every draft lands as an unpublished WordPress draft at most. Going
live requires you to type the word "publish," have the bot read back
exactly what it's about to post, and then type "yes." A button tap alone
can't do it.

---

## What a normal morning looks like

```
6:00 AM  Telegram: "3 new decisions worth a look:
         1️⃣ CD Cal: TCPA good-faith defense survives MTD
         2️⃣ 9th Cir: FCRA — no standing for bare § 1681e(b) claim
         3️⃣ ND Cal: file-transfer vendor isn't a CCPA 'business'
         Reply a number to draft, or 'skip'."

You:     1

Bot:     [~90 seconds later]
         "✅ VERIFIED — 4/4 quotes checked, citation confirmed,
         holding direction confirmed, independent audit clean.
         Preview: [link]
         Reply 'publish', or tell me what to change."

You:     publish

Bot:     "Publishing to consumerfinanceprivacycounsel.com. Reply YES to confirm."

You:     yes

Bot:     "✅ Published: [link]"
```

If nothing that day is worth writing about, it stays silent — no "nothing
today" noise every morning. On Saturdays, it sends a recap of anything you
skipped during the week, in case you want a second look.

---

## What this costs, and why each piece is necessary

| Service | Cost | What it actually buys |
|---|---|---|
| **Railway** (server hosting) | ~$10-15/mo | The system needs to run continuously — checking for new cases, sending your morning digest at 6am, listening for your Telegram replies. That requires a server running 24/7 somewhere; Railway is where it lives. This isn't optional for a "your blog post is ready before you leave the driveway" experience — a laptop that's asleep can't do that. |
| **Free Law Project membership** (CourtListener) | ~$10/mo | CourtListener is a nonprofit; its free tier is rate-limited and doesn't include real-time alerts for *unpublished* district-court decisions — which, worth being honest about, is most of what you actually wrote about historically. A test run without this membership found only about 20% of a sample of your old cases were findable for free; the membership unlocks the search tools that close most of that gap. |
| **Anthropic API** (the AI itself) | ~$10-20/mo | This is the actual "thinking" — reading the opinion, writing the draft, running the fact-checks. It's billed per use, roughly 20-40 cents per drafted post depending on how long the opinion is. This is a different, metered version of the same AI I'm built on — a deployed server can't use my regular subscription-based access the way a person typing at a keyboard can, so this is the one piece that has genuinely no free alternative. |
| **Telegram** | $0 | Free, and it's the one piece with zero recurring cost — no reason to consider a paid texting service. |

**Total: roughly $25-40/month.** For context, that's less than an hour of
your own billing time, for something that used to take you real writing
time on top of your caseload.

---

## What's already built and proven, versus what's still ahead

**Already working, tested against real, live court cases (not made-up
examples) during development:**
- Finding and downloading real current cases from public court records
- Learning your writing style from your old blog archive
- Drafting a post in that style from a real opinion
- All four verification layers, including catching deliberately-planted
  errors in test drafts
- The conversation flow (draft → review → publish) as a set of tested rules

**Needs your input before it goes further:**
- Creating the actual accounts (Railway, CourtListener, Anthropic,
  a free Telegram bot) — a short, one-time setup, mostly clicking through
  account creation and pasting a couple of keys
- A "shadow week" where it runs quietly and you compare its picks against
  what you'd actually have blogged about, before it ever touches the real
  site
- Your explicit sign-off before it's connected to the live WordPress blog
  at all — that connection is the very last step, and only happens after
  you've seen it work

The technical setup instructions live in a repository your own AI assistant
can read and walk through with you step by step — every account you need
to create is called out explicitly, and nothing happens without you.
