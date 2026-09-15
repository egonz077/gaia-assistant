-- migrations/007_revoked_notified_at.sql

-- "Have we already told them?" needs its own answer. The digest first tried
-- to derive it from last_digest_on (a DATE) compared against revoked_at: with
-- the write happening AFTER a successful send, revoked_on < last_digest_on
-- re-announced the next morning whenever the grant lapsed on the same day as
-- a digest, and a strict > silently ate a revocation that happened later that
-- same morning. A DATE cannot express "have we said this" -- only "what day
-- is it" -- so this is a fact of its own, stamped exactly once, at the moment
-- it is said.
ALTER TABLE google_accounts ADD COLUMN revoked_notified_at TIMESTAMPTZ;
