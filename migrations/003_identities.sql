-- 003_identities.sql — per-user identity docs + email mapping.
--
-- An identity is a markdown/text profile for a real user that gets prepended
-- to the agent's system prompt. A single user can have many emails (personal
-- + satellite messenger + work) via identity_emails.
--
-- Keyed by user_id (slug) rather than email so multi-email users stay one row.

CREATE TABLE IF NOT EXISTS identities (
    user_id    TEXT PRIMARY KEY,
    title      TEXT,
    body       TEXT NOT NULL,
    tags       TEXT[] NOT NULL DEFAULT '{}',
    metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS identity_emails (
    email      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES identities(user_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS identity_emails_user_idx ON identity_emails (user_id);
