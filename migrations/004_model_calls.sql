-- Renamed from llm_calls. Nova-3, which transcribes voice notes, is a
-- dedicated ASR model and not an LLM — the old name became wrong the moment
-- transcription rows joined the table. The table was one day old when this
-- ran; RENAME preserves every row and every index.
ALTER TABLE llm_calls RENAME TO model_calls;
ALTER INDEX idx_llm_calls_day RENAME TO idx_model_calls_day;

-- NULL for token-billed calls. Transcription bills in audio-seconds, and every
-- other column in this table assumes tokens, so the unit a row is priced in is
-- what this column records. stats.row_cost branches on it, which is also why a
-- transcription row carrying zeros in the token columns cannot be mistaken for
-- a free one.
ALTER TABLE model_calls ADD COLUMN audio_seconds NUMERIC(10,2);
