PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    guild_id TEXT NULL,
    clock_in_utc TEXT NOT NULL,
    clock_out_utc TEXT NULL,
    rendered_minutes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('active', 'valid', 'invalidated')),
    edited_by_user_id TEXT NULL,
    edited_by_display_name TEXT NULL,
    edited_at_utc TEXT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_shifts_one_active_per_user
    ON shifts(user_id)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_shifts_user_status
    ON shifts(user_id, status, clock_in_utc);

CREATE TABLE IF NOT EXISTS shift_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id INTEGER NULL,
    actor_user_id TEXT NOT NULL,
    actor_display_name TEXT NOT NULL,
    action TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (shift_id) REFERENCES shifts(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS shift_message_refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id INTEGER NOT NULL,
    ref_type TEXT NOT NULL,
    guild_id TEXT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    thread_id TEXT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE(shift_id, ref_type, channel_id, message_id),
    FOREIGN KEY (shift_id) REFERENCES shifts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS shift_change_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id INTEGER NOT NULL,
    requester_user_id TEXT NOT NULL,
    requester_display_name TEXT NOT NULL,
    request_type TEXT NOT NULL CHECK(request_type IN ('adjust', 'void')),
    requested_clock_in_utc TEXT NULL,
    requested_clock_out_utc TEXT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'denied')),
    admin_message_channel_id TEXT NULL,
    admin_message_id TEXT NULL,
    decided_by_user_id TEXT NULL,
    decided_by_display_name TEXT NULL,
    decided_at_utc TEXT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (shift_id) REFERENCES shifts(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_pending_request_per_shift
    ON shift_change_requests(shift_id)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS admin_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    added_by_user_id TEXT NULL,
    added_by_display_name TEXT NULL,
    added_at_utc TEXT NOT NULL,
    removed_by_user_id TEXT NULL,
    removed_by_display_name TEXT NULL,
    removed_at_utc TEXT NULL
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
