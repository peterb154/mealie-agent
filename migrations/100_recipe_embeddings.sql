-- 100_recipe_embeddings.sql — local vector mirror of Mealie recipes.
--
-- Mealie owns the authoritative recipe data. We mirror ONLY what we need
-- for semantic search: the Mealie recipe ID (so we can fetch full detail
-- via Mealie's API on a hit), a snippet of text we embedded, a 1024-dim
-- Titan-v2 embedding, and a timestamp so the sync job can do incremental
-- updates.
--
-- No JOINs against Mealie's other tables — those live in Mealie's DB.

CREATE TABLE IF NOT EXISTS recipe_embeddings (
    mealie_recipe_id  TEXT PRIMARY KEY,
    slug              TEXT NOT NULL,
    name              TEXT NOT NULL,
    snippet           TEXT NOT NULL,       -- the text we actually embedded
    embedding         vector(1024) NOT NULL,
    source_updated_at TIMESTAMPTZ,         -- Mealie recipe.date_updated
    synced_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS recipe_embeddings_slug_idx
    ON recipe_embeddings (slug);

CREATE INDEX IF NOT EXISTS recipe_embeddings_source_updated_idx
    ON recipe_embeddings (source_updated_at);

-- IVFFlat for approximate KNN. 5346 recipes fit comfortably in a single
-- list bucket but set lists=50 for headroom as the corpus grows.
-- Run ANALYZE after bulk ingest for the planner to pick this.
CREATE INDEX IF NOT EXISTS recipe_embeddings_vec_idx
    ON recipe_embeddings USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 50);
