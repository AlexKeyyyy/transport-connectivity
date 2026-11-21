CREATE TABLE IF NOT EXISTS data_source (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS data_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES data_source(source_id),
    sha256 TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    manifest JSONB NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS load_run (
    load_run_id SERIAL PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES data_snapshot(snapshot_id),
    status TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    code_commit TEXT NOT NULL,
    rows_read INTEGER NOT NULL,
    rows_written INTEGER NOT NULL,
    rows_rejected INTEGER NOT NULL,
    duplicates_removed INTEGER NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS rejected_row (
    load_run_id INTEGER NOT NULL REFERENCES load_run(load_run_id) ON DELETE CASCADE,
    source_row_number INTEGER NOT NULL,
    reject_reason TEXT NOT NULL,
    detail TEXT,
    raw_json JSONB NOT NULL,
    PRIMARY KEY (load_run_id, source_row_number)
);

CREATE INDEX IF NOT EXISTS idx_load_run_snapshot ON load_run(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_rejected_row_run ON rejected_row(load_run_id);