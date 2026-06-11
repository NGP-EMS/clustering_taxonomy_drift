-- 001_backfill_schema.sql
-- Adds four tables for column-level backfill auditing.
-- Safe to re-run: all statements use IF NOT EXISTS.

-- ── 1. taxonomy_backfill_runs ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS taxonomy_backfill_runs (
    backfill_run_id         TEXT        NOT NULL PRIMARY KEY,
    mode                    TEXT        NOT NULL DEFAULT 'backfill',
    dry_run                 BOOLEAN     NOT NULL DEFAULT TRUE,
    source_schema           TEXT        NOT NULL DEFAULT 'public',
    source_table            TEXT        NOT NULL DEFAULT 'fact_call_classification',
    selected_fields         TEXT[]      NOT NULL,
    worker_count            INTEGER     NOT NULL,
    batch_size              INTEGER     NOT NULL,
    update_page_size        INTEGER     NOT NULL,
    include_already_updated BOOLEAN     NOT NULL DEFAULT FALSE,
    started_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at             TIMESTAMPTZ,
    status                  TEXT        NOT NULL DEFAULT 'RUNNING'
                                CONSTRAINT tbr_status_check
                                CHECK (status IN ('RUNNING','DONE','FAILED','DRY_RUN_DONE')),
    rows_scanned            BIGINT      NOT NULL DEFAULT 0,
    rows_changed            BIGINT      NOT NULL DEFAULT 0,
    rows_unchanged          BIGINT      NOT NULL DEFAULT 0,
    rows_error              BIGINT      NOT NULL DEFAULT 0,
    rows_pending_before     BIGINT,
    rows_pending_after      BIGINT,
    summary_json            JSONB,
    error_message           TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 2. taxonomy_backfill_row_audit ────────────────────────────────────────────
-- One row per STAGE row per run.
-- JSONB for value/field arrays so the schema never needs to expand.
CREATE TABLE IF NOT EXISTS taxonomy_backfill_row_audit (
    id               BIGSERIAL   NOT NULL PRIMARY KEY,
    backfill_run_id  TEXT        NOT NULL
                         REFERENCES taxonomy_backfill_runs(backfill_run_id),
    stage_row_id     TEXT        NOT NULL,
    call_id          TEXT,
    row_status       TEXT        NOT NULL
                         CONSTRAINT tbra_status_check
                         CHECK (row_status IN (
                             'CHANGED',
                             'UNCHANGED',
                             'ERROR',
                             'SKIPPED_ALREADY_UPDATED',
                             'SKIPPED_NO_TAXONOMY_FIELDS'
                         )),
    changed_fields   JSONB,
    unchanged_fields JSONB,
    unmapped_fields  JSONB,
    error_message    TEXT,
    old_row_hash     TEXT,
    new_row_hash     TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tbra_run
    ON taxonomy_backfill_row_audit(backfill_run_id);
CREATE INDEX IF NOT EXISTS idx_tbra_stage_row
    ON taxonomy_backfill_row_audit(stage_row_id);
CREATE INDEX IF NOT EXISTS idx_tbra_status
    ON taxonomy_backfill_row_audit(row_status);
CREATE INDEX IF NOT EXISTS idx_tbra_run_status
    ON taxonomy_backfill_row_audit(backfill_run_id, row_status);

-- ── 3. taxonomy_backfill_field_audit ─────────────────────────────────────────
-- One row per STAGE row × field per run.
-- old_value / new_value are JSONB so array fields and scalar fields are
-- stored uniformly without text-serialisation hacks.
CREATE TABLE IF NOT EXISTS taxonomy_backfill_field_audit (
    id                   BIGSERIAL   NOT NULL PRIMARY KEY,
    backfill_run_id      TEXT        NOT NULL
                             REFERENCES taxonomy_backfill_runs(backfill_run_id),
    stage_row_id         TEXT        NOT NULL,
    call_id              TEXT,
    field_name           TEXT        NOT NULL,
    old_value            JSONB,
    new_value            JSONB,
    changed              BOOLEAN     NOT NULL DEFAULT FALSE,
    field_status         TEXT        NOT NULL
                             CONSTRAINT tbfa_status_check
                             CHECK (field_status IN (
                                 'CHANGED',
                                 'UNCHANGED',
                                 'UNMAPPED',
                                 'AMBIGUOUS',
                                 'EMPTY',
                                 'ERROR'
                             )),
    mapping_method       TEXT,
    mapped_display_names JSONB,
    unmapped_labels      JSONB,
    ambiguous_labels     JSONB,
    notes                TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tbfa_run
    ON taxonomy_backfill_field_audit(backfill_run_id);
CREATE INDEX IF NOT EXISTS idx_tbfa_field
    ON taxonomy_backfill_field_audit(field_name);
CREATE INDEX IF NOT EXISTS idx_tbfa_status
    ON taxonomy_backfill_field_audit(field_status);
CREATE INDEX IF NOT EXISTS idx_tbfa_stage_row
    ON taxonomy_backfill_field_audit(stage_row_id);
CREATE INDEX IF NOT EXISTS idx_tbfa_run_field
    ON taxonomy_backfill_field_audit(backfill_run_id, field_name);

-- ── 4. taxonomy_unresolved_label_queue ───────────────────────────────────────
-- Durable queue for labels that had no approved mapping.
-- UNIQUE on (field_name, normalized_label): upserts merge occurrences across runs.
-- raw_label stores the most-recent raw example; source_examples holds variants.
-- resolver_status stays NULL until the resolver script decides.
CREATE TABLE IF NOT EXISTS taxonomy_unresolved_label_queue (
    id                         BIGSERIAL    NOT NULL PRIMARY KEY,
    field_name                 TEXT         NOT NULL,
    raw_label                  TEXT         NOT NULL,
    normalized_label           TEXT         NOT NULL,
    occurrence_count           INTEGER      NOT NULL DEFAULT 1,
    distinct_call_count        INTEGER      NOT NULL DEFAULT 1,
    first_seen_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_seen_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_examples            JSONB,
    top_candidate_clusters     JSONB,
    resolver_status            TEXT
                                   CONSTRAINT tulq_resolver_status_check
                                   CHECK (resolver_status IN (
                                       'MAP_TO_EXISTING',
                                       'ANOMALY',
                                       'PROMOTE'
                                   )),
    target_cluster_id          TEXT,
    target_display_name        TEXT,
    similarity_score           NUMERIC(6,4),
    evidence_json              JSONB,
    actor_guard_status         TEXT,
    contradiction_guard_status TEXT,
    created_at                 TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT tulq_field_norm_unique UNIQUE (field_name, normalized_label)
);

CREATE INDEX IF NOT EXISTS idx_tulq_field
    ON taxonomy_unresolved_label_queue(field_name);
CREATE INDEX IF NOT EXISTS idx_tulq_resolver_status
    ON taxonomy_unresolved_label_queue(resolver_status);
CREATE INDEX IF NOT EXISTS idx_tulq_last_seen
    ON taxonomy_unresolved_label_queue(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_tulq_unresolved
    ON taxonomy_unresolved_label_queue(field_name, resolver_status)
    WHERE resolver_status IS NULL;
