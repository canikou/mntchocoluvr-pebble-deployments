PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS employees (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    rank TEXT NOT NULL DEFAULT 'Intern',
    active INTEGER NOT NULL DEFAULT 1,
    added_by_user_id TEXT NULL,
    added_by_display_name TEXT NULL,
    added_at_utc TEXT NOT NULL,
    updated_by_user_id TEXT NULL,
    updated_by_display_name TEXT NULL,
    updated_at_utc TEXT NOT NULL,
    removed_by_user_id TEXT NULL,
    removed_by_display_name TEXT NULL,
    removed_at_utc TEXT NULL
);

ALTER TABLE shifts ADD COLUMN payout_snapshot_id INTEGER NULL;
ALTER TABLE service_logs ADD COLUMN payout_snapshot_id INTEGER NULL;

CREATE INDEX IF NOT EXISTS idx_shifts_payout_snapshot
    ON shifts(payout_snapshot_id, user_id, status);

CREATE INDEX IF NOT EXISTS idx_service_logs_payout_snapshot
    ON service_logs(payout_snapshot_id, user_id, status);

CREATE TABLE IF NOT EXISTS payout_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_type TEXT NOT NULL CHECK(snapshot_type IN ('general', 'early')),
    status TEXT NOT NULL CHECK(status IN ('active', 'reverted')),
    created_by_user_id TEXT NOT NULL,
    created_by_display_name TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    cutoff_start_utc TEXT NOT NULL,
    cutoff_end_utc TEXT NOT NULL,
    include_skipped INTEGER NOT NULL DEFAULT 0,
    reverted_by_user_id TEXT NULL,
    reverted_by_display_name TEXT NULL,
    reverted_at_utc TEXT NULL
);

CREATE TABLE IF NOT EXISTS payout_snapshot_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    rank TEXT NOT NULL,
    duty_minutes INTEGER NOT NULL DEFAULT 0,
    response_count INTEGER NOT NULL DEFAULT 0,
    vital_count INTEGER NOT NULL DEFAULT 0,
    payout_total INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES payout_snapshots(id) ON DELETE CASCADE
);
