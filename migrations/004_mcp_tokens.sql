-- 004_mcp_tokens.sql — per-user Mealie API tokens for the MCP server.
--
-- The mcp-gateway forwards the Cloudflare Access email as X-MCP-User;
-- this table maps that email to a long-lived Mealie API token so the MCP
-- server can act as that user (Mealie enforces RBAC per token).
--
-- Provisioning: the user creates an API token in Mealie (user settings →
-- API tokens), then an admin inserts the row:
--   INSERT INTO mcp_user_tokens (email, mealie_token)
--   VALUES (lower('person@example.com'), '<token>');

CREATE TABLE IF NOT EXISTS mcp_user_tokens (
    email        TEXT PRIMARY KEY,   -- CF Access email, stored lowercased
    mealie_token TEXT NOT NULL,      -- long-lived Mealie API token for that user
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
