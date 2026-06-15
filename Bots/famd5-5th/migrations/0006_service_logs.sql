PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS user_forum_threads (
    thread_type TEXT NOT NULL,
    user_id TEXT NOT NULL,
    guild_id TEXT NULL,
    forum_channel_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    thread_name TEXT NOT NULL DEFAULT '',
    summary_message_id TEXT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY(thread_type, user_id)
);

CREATE TABLE IF NOT EXISTS service_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_kind TEXT NOT NULL CHECK(log_kind IN ('response', 'vital')),
    user_id TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    guild_id TEXT NULL,
    postal TEXT NOT NULL DEFAULT '',
    response_type TEXT NULL CHECK(response_type IS NULL OR response_type IN ('DISTRESS', 'ROBBERY')),
    responders_text TEXT NOT NULL DEFAULT '',
    response_count INTEGER NOT NULL DEFAULT 0,
    treatment_count INTEGER NOT NULL DEFAULT 0,
    revival_count INTEGER NOT NULL DEFAULT 0,
    bodybag_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('pending_proof', 'valid', 'invalidated')),
    edited_by_user_id TEXT NULL,
    edited_by_display_name TEXT NULL,
    edited_at_utc TEXT NULL,
    edit_summary TEXT NOT NULL DEFAULT '',
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    proof_received_at_utc TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_service_logs_user_kind_status
    ON service_logs(user_id, log_kind, status, created_at_utc);

CREATE TABLE IF NOT EXISTS service_log_proofs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_log_id INTEGER NOT NULL,
    local_path TEXT NOT NULL,
    original_filename TEXT NOT NULL DEFAULT '',
    content_type TEXT NULL,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (service_log_id) REFERENCES service_logs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS service_log_message_refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_log_id INTEGER NOT NULL,
    ref_type TEXT NOT NULL,
    guild_id TEXT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    thread_id TEXT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE(service_log_id, ref_type, channel_id, message_id),
    FOREIGN KEY (service_log_id) REFERENCES service_logs(id) ON DELETE CASCADE
);

