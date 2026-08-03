-- Evidence locker + pipeline state.
--
-- Portable across SQLite (local dev) and Postgres (Railway deploy): no
-- native UUID/JSONB/BYTEA types. IDs are TEXT (uuid4 hex, generated in
-- Python), JSON columns are TEXT (json.dumps/loads at the app layer), binary
-- blobs are BLOB (SQLite) / BYTEA (Postgres) — the pipeline.db adapter picks
-- the right DDL flavor per DATABASE_URL scheme.
--
-- The locker contract: cases/case_sources/artifacts are INSERT-only from the
-- app's normal role. Nothing in the drafting stage may read outside a named
-- case's own artifacts.content_text. See pipeline/db/README.md.

CREATE TABLE IF NOT EXISTS cases (
    id              TEXT PRIMARY KEY,
    canonical_key   TEXT NOT NULL UNIQUE,
    case_name       TEXT NOT NULL,
    court_id        TEXT NOT NULL,          -- cacd|cand|casd|caed|ca9|scotus|cal|calctapp|...
    docket_number   TEXT,
    docket_number_norm TEXT,                -- non-alphanumerics stripped, for join/dedupe
    judge           TEXT,
    date_filed      TEXT,                   -- ISO date
    cl_cluster_id   INTEGER,
    cl_docket_id    INTEGER,
    westlaw_cite    TEXT,
    status          TEXT NOT NULL DEFAULT 'captured',
        -- captured -> scored -> digested -> draft_requested -> drafted
        -- -> needs_review -> approved -> published | skipped
    score           REAL,
    score_breakdown TEXT,                   -- json
    first_seen_at   TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_sources (
    id          TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL REFERENCES cases(id),
    source      TEXT NOT NULL,   -- courtlistener_alert|courtlistener_sweep|calcourts|ca9_rss|govinfo|cfpb|fcc|manual
    source_url  TEXT NOT NULL,
    raw_payload TEXT NOT NULL,   -- json, verbatim
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id             TEXT PRIMARY KEY,
    case_id        TEXT NOT NULL REFERENCES cases(id),
    kind           TEXT NOT NULL,  -- opinion_text|opinion_pdf|docket_entry_pdf|agency_release
    sha256         TEXT NOT NULL,
    byte_size      INTEGER NOT NULL,
    content_text   TEXT,
    content_bytes  BLOB,
    source_url     TEXT NOT NULL,
    fetched_at     TEXT NOT NULL,
    UNIQUE (case_id, kind, sha256)
);

CREATE TABLE IF NOT EXISTS fact_sheets (
    case_id           TEXT PRIMARY KEY REFERENCES cases(id),
    posture           TEXT,
    holding_direction TEXT,
    statutes          TEXT,   -- json list
    key_passages      TEXT,   -- json list of {text, start, end}
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id                TEXT PRIMARY KEY,
    case_id           TEXT NOT NULL REFERENCES cases(id),
    revision          INTEGER NOT NULL DEFAULT 1,
    title             TEXT,
    slug              TEXT,
    intro             TEXT,
    blocks            TEXT,    -- json
    closing_citation  TEXT,
    categories        TEXT,    -- json list
    excerpt           TEXT,
    body_rendered     TEXT,
    artifact_shas      TEXT,   -- json list — evidence this draft was allowed to see
    gate_status       TEXT,    -- pending|pass|warn|reject|needs_review
    provenance        TEXT,    -- json, see verify_gate.provenance schema
    wp_post_id        INTEGER,
    wp_status         TEXT,    -- draft|publish
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
    id         TEXT PRIMARY KEY,
    sent_at    TEXT,
    items      TEXT NOT NULL,  -- json list of {case_id, rank, hook}
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id            TEXT PRIMARY KEY,
    draft_id      TEXT NOT NULL REFERENCES drafts(id),
    telegram_user TEXT NOT NULL,
    verbatim_text TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sms_log (
    id            TEXT PRIMARY KEY,
    direction     TEXT NOT NULL,  -- in|out
    telegram_user TEXT,
    text          TEXT NOT NULL,
    parsed_intent TEXT,           -- json
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    request     TEXT NOT NULL,
    cost        INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_budget (
    source     TEXT NOT NULL,
    day        TEXT NOT NULL,   -- YYYY-MM-DD
    used       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, day)
);

CREATE TABLE IF NOT EXISTS dedupe_review (
    id          TEXT PRIMARY KEY,
    case_id_a   TEXT NOT NULL,
    case_id_b   TEXT NOT NULL,
    similarity  REAL NOT NULL,
    reason      TEXT NOT NULL,
    resolved    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status);
CREATE INDEX IF NOT EXISTS idx_cases_court ON cases(court_id);
CREATE INDEX IF NOT EXISTS idx_cases_docket_norm ON cases(docket_number_norm);
CREATE INDEX IF NOT EXISTS idx_case_sources_case ON case_sources(case_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_case ON artifacts(case_id);
CREATE INDEX IF NOT EXISTS idx_drafts_case ON drafts(case_id);
