-- 005_memories_updated_at.sql — track when a memory was last rewritten.
--
-- ``update`` preserves created_at on purpose: a standing preference
-- learned in April is still an April fact even after the wording is
-- tightened. But that leaves the audit view (list_notes) unable to tell
-- "written in April" from "written in April, rewritten today", which
-- matters once notes are actively curated rather than only appended.
--
-- NULL means never edited since creation. Backfilling it to created_at
-- would assert an edit that never happened, so existing rows stay NULL.

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
