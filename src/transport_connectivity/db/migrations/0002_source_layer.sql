CREATE TABLE IF NOT EXISTS data_source (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    license TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS data_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES data_source(source_id),
    file_name TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    record_count INTEGER,
    manifest JSONB NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS load_run (
    load_run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_id TEXT NOT NULL REFERENCES data_snapshot(snapshot_id),
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'success_with_rejections', 'failed')),
    contract_version TEXT NOT NULL,
    code_commit TEXT NOT NULL,
    rows_read INTEGER,
    rows_written INTEGER,
    rows_rejected INTEGER,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    CHECK (rows_read = rows_written + rows_rejected)
);

CREATE TABLE IF NOT EXISTS rejected_row (
    load_run_id UUID NOT NULL REFERENCES load_run(load_run_id) ON DELETE CASCADE,
    source_row_number INTEGER NOT NULL,
    reject_reason TEXT NOT NULL,
    detail TEXT,
    raw_json JSONB NOT NULL,
    PRIMARY KEY (load_run_id, source_row_number)
);

CREATE INDEX IF NOT EXISTS idx_load_run_snapshot ON load_run(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_data_snapshot_source ON data_snapshot(source_id);