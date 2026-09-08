-- Requires: CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto; -- gen_random_uuid

-- ============ CONTACTS / LEADS ============
CREATE TABLE contacts (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL,
    phone        TEXT,
    email        TEXT,
    -- rolling LLM-maintained profile: preferences, family, budget, quirks
    profile      TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TYPE lead_status AS ENUM ('new', 'active', 'under_contract', 'closed', 'lost', 'dormant');

CREATE TABLE leads (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id       UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    description      TEXT NOT NULL,            -- "buying in Coral Gables, ~600k"
    status           lead_status NOT NULL DEFAULT 'new',
    last_touch_at    TIMESTAMPTZ,              -- last real interaction
    next_action_at   TIMESTAMPTZ,              -- when a follow-up is due
    next_action_note TEXT,                     -- "send Friday listing update"
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_leads_due ON leads (next_action_at)
    WHERE status IN ('new', 'active');

-- ============ MEETINGS / NOTES ============
CREATE TYPE source_kind AS ENUM ('text', 'photo_notes', 'transcript');

CREATE TABLE meetings (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    happened_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    source       source_kind NOT NULL,
    raw_input    TEXT,                          -- original text or vision transcription
    summary      TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- many-to-many: a meeting can involve several contacts
CREATE TABLE meeting_contacts (
    meeting_id UUID REFERENCES meetings(id) ON DELETE CASCADE,
    contact_id UUID REFERENCES contacts(id) ON DELETE CASCADE,
    PRIMARY KEY (meeting_id, contact_id)
);

-- ============ COMMITMENTS / ACTION ITEMS ============
CREATE TABLE commitments (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    meeting_id  UUID REFERENCES meetings(id) ON DELETE SET NULL,
    contact_id  UUID REFERENCES contacts(id) ON DELETE SET NULL,
    description TEXT NOT NULL,
    due_at      TIMESTAMPTZ,
    done_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_commitments_open ON commitments (due_at) WHERE done_at IS NULL;

-- ============ SEMANTIC MEMORY ============
-- one row per chunk (meeting summary, note transcription, important message)
CREATE TABLE memory_chunks (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    meeting_id UUID REFERENCES meetings(id) ON DELETE CASCADE,
    contact_id UUID REFERENCES contacts(id) ON DELETE CASCADE,
    content    TEXT NOT NULL,
    embedding  vector(1024),                    -- match your embedding model dims
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_memory_embedding ON memory_chunks
    USING hnsw (embedding vector_cosine_ops);

-- ============ CONVERSATION LOG (WhatsApp thread) ============
CREATE TABLE messages (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    wa_msg_id  TEXT UNIQUE,                     -- WhatsApp id, for dedup
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
