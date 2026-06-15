PRAGMA foreign_keys = ON;

ALTER TABLE shifts ADD COLUMN break_minutes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE shifts ADD COLUMN unpaid_break_minutes INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS shift_breaks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id INTEGER NOT NULL,
    break_start_utc TEXT NOT NULL,
    break_end_utc TEXT NULL,
    credited_minutes INTEGER NOT NULL DEFAULT 0,
    unpaid_minutes INTEGER NOT NULL DEFAULT 0,
    warning_sent_at_utc TEXT NULL,
    final_prompt_sent_at_utc TEXT NULL,
    final_prompt_message_channel_id TEXT NULL,
    final_prompt_message_id TEXT NULL,
    auto_clocked_out INTEGER NOT NULL DEFAULT 0,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (shift_id) REFERENCES shifts(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_break_per_shift
    ON shift_breaks(shift_id)
    WHERE break_end_utc IS NULL;
