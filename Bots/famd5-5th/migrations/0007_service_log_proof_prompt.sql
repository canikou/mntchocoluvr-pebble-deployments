PRAGMA foreign_keys = ON;

ALTER TABLE service_logs ADD COLUMN proof_prompt_channel_id TEXT NULL;
ALTER TABLE service_logs ADD COLUMN proof_prompt_message_id TEXT NULL;

