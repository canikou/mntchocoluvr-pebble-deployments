PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS user_log_threads (
    user_id TEXT PRIMARY KEY,
    guild_id TEXT NULL,
    forum_channel_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    thread_name TEXT NOT NULL DEFAULT '',
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

