-- 002_prompts.sql
-- Prompts live in the DB, not on disk. Seed from filesystem defaults on first
-- boot via strands_pg.prompts.PgPromptStore.seed_from_dir().

CREATE TABLE IF NOT EXISTS prompts (
    name       TEXT PRIMARY KEY,
    body       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
