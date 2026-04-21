-- 101_recipe_rating.sql — mirror Mealie's `rating` (0-5) onto our local
-- pgvector table so the agent can filter/sort by rating without an extra
-- API round-trip per search result.
--
-- Additive only (ADD COLUMN IF NOT EXISTS). Safe to re-run.

ALTER TABLE recipe_embeddings
    ADD COLUMN IF NOT EXISTS rating REAL;

-- Partial index on rated rows only; most of the library will be null.
CREATE INDEX IF NOT EXISTS recipe_embeddings_rating_idx
    ON recipe_embeddings (rating DESC NULLS LAST)
    WHERE rating IS NOT NULL;
