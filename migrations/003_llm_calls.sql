-- One row per model call. Counts only, never prompt or completion text:
-- this is the one table in the schema that can be read by anyone without
-- exposing a client, which is what makes the report safe to share.
--
-- No `visibility` column, deliberately. tests/test_scope.py asserts that the
-- set of tables carrying one equals DOMAIN_TABLES exactly.
CREATE TABLE llm_calls (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job          TEXT NOT NULL,          -- 'turn' | 'digest' | 'consolidation'
    -- SET NULL, not the RESTRICT the domain tables use: telemetry should
    -- outlive a roster change, and it carries nothing worth protecting.
    -- Nullable also because consolidation runs for no user at all.
    user_id      UUID REFERENCES users(id) ON DELETE SET NULL,
    model        TEXT NOT NULL,
    input_tokens                INT NOT NULL,
    output_tokens               INT NOT NULL,
    cache_creation_input_tokens INT NOT NULL DEFAULT 0,
    cache_read_input_tokens     INT NOT NULL DEFAULT 0,
    stop_reason  TEXT,
    duration_ms  INT,
    -- Not in the design doc, and required by it: §5.1 asks for "calls per
    -- turn -- the distribution, since 8 is the cap and a turn hitting it
    -- returns FALLBACK_TEXT", and the spec's own schema has no way to group
    -- the calls of one turn. Generated per turn by run_agent and shared by
    -- that turn's iterations. NULL for single-call jobs, where grouping would
    -- mean nothing -- so a digest must never be counted as a one-call turn.
    turn_id      UUID,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_llm_calls_day ON llm_calls (created_at DESC);
