-- 001_init.sql
-- Baseline schema: extensions + session tables + memory table.
-- Applied by strands_pg.migrate.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- Sessions
-- Messages and agent state are stored as JSONB. We keep the shape loose so
-- Strands' own SessionMessage/SessionAgent/Session dataclasses can round-trip
-- via to_dict/from_dict without schema churn.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS session_agents (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    agent_id   TEXT NOT NULL,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, agent_id)
);

CREATE TABLE IF NOT EXISTS session_messages (
    session_id TEXT NOT NULL,
    agent_id   TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, agent_id, message_id),
    FOREIGN KEY (session_id, agent_id)
        REFERENCES session_agents(session_id, agent_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS session_messages_agent_idx
    ON session_messages (session_id, agent_id, message_id);

-- ---------------------------------------------------------------------------
-- Memory
-- One row = one remembered fact. ``namespace`` partitions memory per-user /
-- per-topic inside a single agent. Embeddings are 1024-dim by default (Titan v2
-- / Cohere embed-english-v3); swap the dim at install time if you change model.
-- HNSW index for KNN search.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS memories (
    id         BIGSERIAL PRIMARY KEY,
    namespace  TEXT NOT NULL DEFAULT 'default',
    text       TEXT NOT NULL,
    metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding  vector(1024),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS memories_namespace_idx ON memories (namespace);
CREATE INDEX IF NOT EXISTS memories_embedding_idx
    ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS memories_text_trgm_idx
    ON memories USING gin (text gin_trgm_ops);
