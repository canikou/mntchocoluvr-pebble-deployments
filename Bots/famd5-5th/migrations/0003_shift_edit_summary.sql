PRAGMA foreign_keys = ON;

ALTER TABLE shifts ADD COLUMN edit_summary TEXT NOT NULL DEFAULT '';
