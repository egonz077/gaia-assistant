CREATE EXTENSION IF NOT EXISTS vector;

CREATE TYPE visibility   AS ENUM ('org', 'private');
CREATE TYPE lead_status  AS ENUM ('new','active','under_contract','closed','lost','dormant');
CREATE TYPE source_kind  AS ENUM ('text','photo_notes','transcript');

-- ============ USERS ============
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    wa_id           TEXT NOT NULL UNIQUE,
    role            TEXT NOT NULL DEFAULT 'agent',
    timezone        TEXT NOT NULL DEFAULT 'America/New_York',
    active          BOOLEAN NOT NULL DEFAULT true,
    last_inbound_at TIMESTAMPTZ,
    last_digest_on  DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ CONTACTS ============
CREATE TABLE contacts (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    visibility visibility NOT NULL DEFAULT 'org',
    name       TEXT NOT NULL,
    phone      TEXT,
    email      TEXT,
    profile    TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_contacts_name ON contacts (lower(name));

-- ============ LEADS ============
CREATE TABLE leads (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    visibility       visibility NOT NULL DEFAULT 'org',
    contact_id       UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    description      TEXT NOT NULL,
    status           lead_status NOT NULL DEFAULT 'new',
    last_touch_at    TIMESTAMPTZ,
    next_action_at   TIMESTAMPTZ,
    next_action_note TEXT,
    nudge_count      INT NOT NULL DEFAULT 0,
    last_nudged_at   TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_leads_due ON leads (user_id, next_action_at)
    WHERE status IN ('new','active');

-- ============ MEETINGS ============
CREATE TABLE meetings (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    visibility  visibility NOT NULL DEFAULT 'org',
    happened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source      source_kind NOT NULL,
    raw_input   TEXT,
    summary     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE meeting_contacts (
    meeting_id UUID REFERENCES meetings(id) ON DELETE CASCADE,
    contact_id UUID REFERENCES contacts(id) ON DELETE CASCADE,
    PRIMARY KEY (meeting_id, contact_id)
);

-- ============ COMMITMENTS ============
CREATE TABLE commitments (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    visibility     visibility NOT NULL DEFAULT 'org',
    meeting_id     UUID REFERENCES meetings(id) ON DELETE SET NULL,
    contact_id     UUID REFERENCES contacts(id) ON DELETE SET NULL,
    description    TEXT NOT NULL,
    due_at         TIMESTAMPTZ,
    done_at        TIMESTAMPTZ,
    nudge_count    INT NOT NULL DEFAULT 0,
    last_nudged_at TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_commitments_open ON commitments (user_id, due_at) WHERE done_at IS NULL;

-- ============ SEMANTIC MEMORY ============
CREATE TABLE memory_chunks (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    visibility visibility NOT NULL DEFAULT 'org',
    meeting_id UUID REFERENCES meetings(id) ON DELETE CASCADE,
    contact_id UUID REFERENCES contacts(id) ON DELETE CASCADE,
    content    TEXT NOT NULL,
    embedding  vector(1024),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_memory_embedding ON memory_chunks USING hnsw (embedding vector_cosine_ops);

-- ============ CONVERSATION LOG ============
-- No visibility column: a user's thread is always owner-only.
CREATE TABLE messages (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    role       TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content    TEXT NOT NULL,
    wa_msg_id  TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_messages_thread ON messages (user_id, created_at DESC);

-- ============ DERIVED VISIBILITY CASCADE ============
-- Reclassifying a meeting as private must not leave org-visible embeddings
-- behind; a stale chunk stays searchable by the whole company.
CREATE OR REPLACE FUNCTION cascade_meeting_visibility() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.visibility IS DISTINCT FROM OLD.visibility THEN
        UPDATE memory_chunks SET visibility = NEW.visibility WHERE meeting_id = NEW.id;
        UPDATE commitments   SET visibility = NEW.visibility WHERE meeting_id = NEW.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_cascade_meeting_visibility
    AFTER UPDATE ON meetings
    FOR EACH ROW EXECUTE FUNCTION cascade_meeting_visibility();
