-- =============================================================================
-- Module 2 Migration: processing_state table
-- Tracks which articles have been processed by the knowledge engine.
-- ZERO changes to Phase 1 tables (sources, articles, scraper_logs).
-- Run this once before starting Module 2 for the first time.
-- =============================================================================

BEGIN;


CREATE TABLE IF NOT EXISTS processing_state (
    id              BIGSERIAL PRIMARY KEY,
    article_id      BIGINT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
        -- pending | processing | completed | failed | skipped: these are the different states an article can have, and helps with maintaining the database state.
    attempts        INT NOT NULL DEFAULT 0,
    last_attempted  TIMESTAMP,
    completed_at    TIMESTAMP,
    neo4j_event_ids TEXT[],          -- UUIDs of Event nodes written to Neo4j
    error_message   TEXT,            -- last error if status = failed
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),

    CONSTRAINT processing_state_article_unique UNIQUE (article_id),
    CONSTRAINT processing_state_status_check
        CHECK (status IN ('pending','processing','completed','failed','skipped'))
);

-- creating indices for better retrieval
-- Index for the pipeline's main fetch query: unprocessed articles ordered by publish date
CREATE INDEX IF NOT EXISTS idx_ps_status_created
    ON processing_state(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ps_article_id
    ON processing_state(article_id);

-- Auto-update updated_at on any row change
CREATE OR REPLACE FUNCTION update_processing_state_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_processing_state_updated_at ON processing_state;
CREATE TRIGGER trg_processing_state_updated_at
    BEFORE UPDATE ON processing_state
    FOR EACH ROW EXECUTE FUNCTION update_processing_state_timestamp();

COMMIT;
