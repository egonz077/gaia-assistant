-- migrations/006_google_workspace.sql

-- The address the OAuth callback must match. A precondition for connecting,
-- never something the callback fills in: if first connect defined the answer,
-- a colleague with a forwarded link would pass the domain check and bind their
-- own account -- and Gaia would then create this developer's events, client
-- names and all, on someone else's calendar.
ALTER TABLE users ADD COLUMN email TEXT UNIQUE;

-- The event for the current next action, not a history. What already happened
-- lives in meetings; a second home for the same truth is how the two drift.
ALTER TABLE leads       ADD COLUMN calendar_event_id TEXT;
ALTER TABLE commitments ADD COLUMN calendar_event_id TEXT;

-- No visibility column: a token is owner-only at every setting, and nobody may
-- read a colleague's. Same reasoning as messages, one table stronger.
-- ON DELETE CASCADE, unlike the RESTRICT used for domain rows: a refresh token
-- outliving its user is a live credential with no owner, which is worse than a
-- blocked delete.
CREATE TABLE google_accounts (
    user_id           UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    google_email      TEXT NOT NULL,
    refresh_token_enc TEXT NOT NULL,
    scopes            TEXT NOT NULL,
    granted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at        TIMESTAMPTZ
);

-- No visibility column: one person's pending workflow, not org content.
-- emails is the list the human actually saw. confirm_invite sends THIS, and
-- takes no addresses of its own, so the model cannot confirm a different list
-- than the one that was read back.
CREATE TABLE pending_invites (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id     TEXT NOT NULL,
    emails       TEXT[] NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    confirmed_at TIMESTAMPTZ
);
CREATE INDEX idx_pending_invites_open ON pending_invites (user_id, expires_at)
    WHERE confirmed_at IS NULL;
