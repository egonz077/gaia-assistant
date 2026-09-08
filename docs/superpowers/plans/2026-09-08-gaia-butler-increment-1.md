# gaia-butler Increment 1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the single-user WhatsApp prototype as a multi-tenant assistant serving every agent at Gaia from one number, with a capability registry, working lead tracking, and a deployable droplet setup.

**Architecture:** Three layers with hard boundaries. `gaia/core/` owns WhatsApp, Postgres, Claude, users and visibility, and knows nothing about real estate. `gaia/capabilities/` owns the domain — each capability is a directory declaring tools plus a prompt fragment, assembled per-user at request time. `gaia/jobs/` holds scheduled entry points. Every domain row carries `user_id` (owner) and `visibility` (`org` by default, `private` opt-in), and one SQL fragment in `core/db/scope.py` is the only place read-filtering lives.

**Tech Stack:** Python 3.12, FastAPI, psycopg3 (async, pooled), Postgres 17 + pgvector, Anthropic SDK, Voyage embeddings, Pillow, Caddy, Docker Compose. Tests: pytest + pytest-asyncio against a real Postgres.

**Spec:** `docs/superpowers/specs/2026-09-08-gaia-butler-design.md`

## Global Constraints

Copied verbatim from the spec. Every task's requirements implicitly include these.

- **Model:** `claude-opus-5`. `max_tokens=8000`. `output_config={"effort": "low"}` for chat turns.
- **Images:** downscaled to **2576px on the long edge**, re-encoded JPEG.
- **Agent loop:** capped at **8 iterations**. Empty text is never sent to WhatsApp.
- **Visibility default:** `org`. Private is opt-in. Derived rows inherit from their parent.
- **Scoping:** every public function in `core/db` takes `user: User` as its first positional argument.
- **Digest:** filters on **ownership** (`user_id = me`), never visibility.
- **Concurrency:** one turn at a time per user. **Single uvicorn worker**, enforced in the Dockerfile `CMD`.
- **Debounce window:** 3 seconds.
- **Postgres image:** `pgvector/pgvector:pg17`, pinned. Never a floating tag.
- **Timezone:** `TZ=America/New_York` on every container; per-user timezones in `users.timezone`.
- **Schema changes:** numbered files in `migrations/` applied by a runner. Never `docker-entrypoint-initdb.d`.
- **Message threads** are always owner-only. `messages` has no `visibility` column.

## Phases

- **Tasks 1-3 — Foundation.** Config, pooled DB, migration runner, schema, scope module.
- **Tasks 4-8 — Data layer.** Users + admin CLI, contacts, meetings, memory, leads, messages. Isolation tests land here.
- **Tasks 9-11 — Butler.** Capability registry, agent loop, WhatsApp client.
- **Task 12 — Walking skeleton.** Deployable end-to-end path with no capabilities. Deploy here.
- **Tasks 13-15 — Capabilities and jobs.** Meetings, leads, the morning digest.
- **Task 16 — Production deployment.** Backups, firewall, restore drill.

---

### Task 1: Project skeleton, settings, and the test harness

**Files:**
- Create: `pyproject.toml`, `gaia/__init__.py`, `gaia/core/__init__.py`, `gaia/core/config.py`
- Create: `tests/__init__.py`, `tests/conftest.py`, `tests/test_config.py`
- Create: `docker-compose.yml` (dev only; production hardening is Task 16)
- Delete: `app/` (the prototype, preserved in commit `89c383d`)

**Interfaces:**
- Consumes: nothing.
- Produces: `gaia.core.config.settings` — a module-level `Settings` instance with fields `database_url: str`, `anthropic_api_key: str`, `voyage_api_key: str`, `wa_access_token: str`, `wa_app_secret: str`, `wa_verify_token: str`, `wa_phone_number_id: str`, `model: str`, `debounce_seconds: float`.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "gaia"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "anthropic>=0.40",
    "voyageai>=0.3",
    "psycopg[binary,pool]>=3.2",
    "pydantic-settings>=2.6",
    "httpx>=0.27",
    "pillow>=11.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.3", "pytest-asyncio>=0.24"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.setuptools.packages.find]
include = ["gaia*"]
```

- [ ] **Step 2: Write the failing test**

`tests/test_config.py`:

```python
from gaia.core.config import Settings


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
    monkeypatch.setenv("WA_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WA_APP_SECRET", "secret")
    monkeypatch.setenv("WA_VERIFY_TOKEN", "verify")
    monkeypatch.setenv("WA_PHONE_NUMBER_ID", "123")

    s = Settings()

    assert s.database_url == "postgresql://u:p@localhost/db"
    assert s.model == "claude-opus-5"       # default
    assert s.debounce_seconds == 3.0        # default
```

- [ ] **Step 3: Run it and watch it fail**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia'`

- [ ] **Step 4: Write `gaia/core/config.py`**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    anthropic_api_key: str
    voyage_api_key: str
    wa_access_token: str
    wa_app_secret: str
    wa_verify_token: str
    wa_phone_number_id: str

    model: str = "claude-opus-5"
    debounce_seconds: float = 3.0


settings = Settings()  # type: ignore[call-arg]
```

Create empty `gaia/__init__.py` and `gaia/core/__init__.py`.

> `settings` is instantiated at import. That is deliberate — a missing
> environment variable should crash at startup, not on the first webhook.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 6: Write the dev compose file**

`docker-compose.yml`:

```yaml
services:
  db:
    image: pgvector/pgvector:pg17
    restart: unless-stopped
    environment:
      POSTGRES_DB: gaia
      POSTGRES_USER: gaia
      POSTGRES_PASSWORD: ${DB_PASSWORD:-devpassword}
      TZ: America/New_York
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U gaia -d gaia"]
      interval: 5s
      timeout: 3s
      retries: 10

volumes:
  pgdata:
```

> Only `db` for now. The app, jobs and caddy services arrive in Task 12 and
> Task 16. Port is bound to `127.0.0.1` so it is never reachable off the box.

- [ ] **Step 7: Delete the prototype**

```bash
git rm -r app schema.sql
```

The prototype is preserved in commit `89c383d` and the design doc records every
defect. Keeping it around invites copy-paste from code known not to work.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml gaia tests docker-compose.yml
git commit -m "feat: project skeleton, settings, dev compose

Settings load from env at import so a missing variable fails at startup
rather than on the first webhook. Removes the prototype, which is preserved
in 89c383d."
```

---

### Task 2: Connection pool and migration runner

**Files:**
- Create: `gaia/core/db/__init__.py`, `gaia/core/db/pool.py`, `gaia/core/db/migrate.py`
- Create: `migrations/001_init.sql` (schema arrives in Task 3; this task creates only the runner's own table)
- Create: `tests/test_migrate.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `gaia.core.config.settings`.
- Produces:
  - `gaia.core.db.pool.get_pool() -> AsyncConnectionPool`
  - `gaia.core.db.pool.tx()` — async context manager yielding a connection with a transaction open and `dict_row` factory.
  - `gaia.core.db.migrate.run_migrations(pool) -> list[str]` — returns names of migrations applied this call.

- [ ] **Step 1: Write the failing test**

`tests/test_migrate.py`:

```python
from gaia.core.db.migrate import run_migrations


async def test_migrations_are_applied_once(pool):
    first = await run_migrations(pool)
    assert "001_init.sql" in first

    second = await run_migrations(pool)
    assert second == []          # already applied, nothing re-runs


async def test_schema_migrations_table_records_names(pool):
    await run_migrations(pool)
    async with pool.connection() as conn:
        rows = await (await conn.execute("SELECT name FROM schema_migrations")).fetchall()
    assert any(r[0] == "001_init.sql" for r in rows)
```

- [ ] **Step 2: Write the test fixtures**

`tests/conftest.py`:

```python
import os
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool

TEST_DSN = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://gaia:devpassword@localhost:5432/gaia_test"
)


@pytest_asyncio.fixture
async def pool():
    """A pool against a freshly dropped-and-recreated test schema."""
    admin_dsn = TEST_DSN.rsplit("/", 1)[0] + "/postgres"
    async with await __import__("psycopg").AsyncConnection.connect(
        admin_dsn, autocommit=True
    ) as conn:
        dbname = TEST_DSN.rsplit("/", 1)[1]
        await conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{dbname}"')

    p = AsyncConnectionPool(TEST_DSN, min_size=1, max_size=4, open=False)
    await p.open(wait=True)
    yield p
    await p.close()
```

> Dropping and recreating per test is slower than truncating but immune to
> leftover state, and this suite is small. Revisit only if it becomes slow.

- [ ] **Step 3: Run it and watch it fail**

Run: `docker compose up -d db && pytest tests/test_migrate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia.core.db'`

- [ ] **Step 4: Write `gaia/core/db/pool.py`**

```python
from contextlib import asynccontextmanager

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from gaia.core.config import settings

_pool: AsyncConnectionPool | None = None


def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(settings.database_url, min_size=1, max_size=8, open=False)
    return _pool


@asynccontextmanager
async def tx(pool: AsyncConnectionPool | None = None):
    """A connection inside a transaction, rows as dicts.

    Commits on clean exit, rolls back on exception.
    """
    p = pool or get_pool()
    async with p.connection() as conn:
        conn.row_factory = dict_row
        async with conn.transaction():
            yield conn
```

- [ ] **Step 5: Write `gaia/core/db/migrate.py`**

```python
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name       TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def run_migrations(pool) -> list[str]:
    """Apply pending migrations in filename order. Returns what was applied."""
    applied: list[str] = []
    async with pool.connection() as conn:
        # Serialise across replicas; harmless with one.
        await conn.execute("SELECT pg_advisory_lock(hashtext('gaia_migrations'))")
        try:
            await conn.execute(_CREATE_TABLE)
            await conn.commit()

            cur = await conn.execute("SELECT name FROM schema_migrations")
            done = {r[0] for r in await cur.fetchall()}

            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in done:
                    continue
                await conn.execute(path.read_text())
                await conn.execute(
                    "INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,)
                )
                await conn.commit()
                applied.append(path.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtext('gaia_migrations'))")
    return applied
```

Create `migrations/001_init.sql` containing only a comment for now:

```sql
-- Schema lands in Task 3.
SELECT 1;
```

Create an empty `gaia/core/db/__init__.py`.

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_migrate.py -v`
Expected: PASS (2 tests)

- [ ] **Step 7: Commit**

```bash
git add gaia/core/db migrations tests
git commit -m "feat: async connection pool and migration runner

Numbered files in migrations/, tracked in schema_migrations, applied under
an advisory lock. Replaces the initdb mount, which only ever ran on an
empty data directory and silently skipped every later schema change."
```

---

### Task 3: Schema and the scope module

**Files:**
- Modify: `migrations/001_init.sql`
- Create: `gaia/core/models.py`, `gaia/core/db/scope.py`
- Create: `tests/test_scope.py`

**Interfaces:**
- Consumes: the migration runner from Task 2.
- Produces:
  - `gaia.core.models.User` — frozen dataclass: `id: UUID`, `name: str`, `wa_id: str`, `role: str`, `timezone: str`, `active: bool`.
  - `gaia.core.db.scope.DOMAIN_TABLES: tuple[str, ...]`
  - `gaia.core.db.scope.visible(alias: str) -> str` — SQL fragment using the named parameter `scope_user_id`.

- [ ] **Step 1: Write the failing test**

`tests/test_scope.py`:

```python
from gaia.core.db.migrate import run_migrations
from gaia.core.db.scope import DOMAIN_TABLES, visible


def test_visible_fragment_uses_named_parameter():
    assert visible("m") == "(m.visibility = 'org' OR m.user_id = %(scope_user_id)s)"


async def test_every_visibility_table_is_registered(pool):
    """A new domain table must be added to DOMAIN_TABLES or the build breaks."""
    await run_migrations(pool)
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT table_name FROM information_schema.columns
               WHERE table_schema = 'public' AND column_name = 'visibility'"""
        )
        found = {r[0] for r in await cur.fetchall()}
    assert found == set(DOMAIN_TABLES), (
        f"tables with a visibility column but not in DOMAIN_TABLES: {found - set(DOMAIN_TABLES)}"
    )
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_scope.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia.core.db.scope'`

- [ ] **Step 3: Write the schema**

Replace `migrations/001_init.sql` entirely:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

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
```

- [ ] **Step 4: Write `gaia/core/models.py`**

```python
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class User:
    id: UUID
    name: str
    wa_id: str
    role: str
    timezone: str
    active: bool

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"
```

- [ ] **Step 5: Write `gaia/core/db/scope.py`**

```python
"""The only place read-filtering lives.

visibility governs reads; user_id governs responsibility. Anything that tells
a person what to do filters on ownership, not on this fragment.
"""

DOMAIN_TABLES: tuple[str, ...] = (
    "contacts",
    "leads",
    "meetings",
    "commitments",
    "memory_chunks",
)


def visible(alias: str) -> str:
    """SQL predicate restricting a domain table to what `scope_user_id` may read."""
    return f"({alias}.visibility = 'org' OR {alias}.user_id = %(scope_user_id)s)"
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_scope.py -v`
Expected: PASS (2 tests)

- [ ] **Step 7: Commit**

```bash
git add migrations gaia/core/models.py gaia/core/db/scope.py tests/test_scope.py
git commit -m "feat: multi-tenant schema and scope module

Every domain table carries user_id and visibility. messages deliberately
has neither a visibility column nor org readability — a thread is a chat
log, not business data.

A trigger cascades meeting visibility to derived chunks and commitments,
so marking a meeting private cannot leave a searchable embedding behind.

test_every_visibility_table_is_registered introspects information_schema,
so adding a domain table without adding isolation coverage fails the build."
```

---

### Task 4: Users repository and the admin CLI

**Files:**
- Create: `gaia/core/db/users.py`, `gaia/core/admin.py`
- Create: `tests/test_users.py`, `tests/test_admin.py`
- Modify: `tests/conftest.py` (add `migrated`, `ana`, `sofia` fixtures)

**Interfaces:**
- Consumes: `tx`, `User`.
- Produces:
  - `create_user(conn, *, name, wa_id, role="agent", timezone="America/New_York") -> User`
  - `get_by_wa_id(conn, wa_id: str) -> User | None` — returns `None` for inactive users.
  - `list_users(conn) -> list[User]`
  - `deactivate(conn, wa_id: str) -> bool`
  - `touch_inbound(conn, user: User) -> None` — sets `last_inbound_at = now()`.

> `create_user`, `get_by_wa_id`, `list_users` and `deactivate` are the four
> functions exempt from the "first argument is `user`" rule, because they
> operate on the roster itself rather than on domain rows. The reflection test
> in Task 5 encodes that exemption explicitly.

- [ ] **Step 1: Add fixtures to `tests/conftest.py`**

Append:

```python
import pytest_asyncio
from psycopg.rows import dict_row

from gaia.core.db import users as users_db
from gaia.core.db.migrate import run_migrations


@pytest_asyncio.fixture
async def migrated(pool):
    await run_migrations(pool)
    return pool


@pytest_asyncio.fixture
async def conn(migrated):
    async with migrated.connection() as c:
        c.row_factory = dict_row
        yield c


@pytest_asyncio.fixture
async def ana(conn):
    return await users_db.create_user(conn, name="Ana", wa_id="13055550001")


@pytest_asyncio.fixture
async def sofia(conn):
    return await users_db.create_user(conn, name="Sofia", wa_id="13055550002")
```

- [ ] **Step 2: Write the failing test**

`tests/test_users.py`:

```python
from gaia.core.db import users as users_db


async def test_create_and_look_up_by_wa_id(conn):
    created = await users_db.create_user(conn, name="Ana", wa_id="13055550001")
    found = await users_db.get_by_wa_id(conn, "13055550001")
    assert found is not None
    assert found.id == created.id
    assert found.name == "Ana"
    assert found.role == "agent"
    assert found.timezone == "America/New_York"


async def test_unknown_number_is_none(conn):
    assert await users_db.get_by_wa_id(conn, "19998887777") is None


async def test_deactivated_user_does_not_resolve(conn, ana):
    assert await users_db.deactivate(conn, ana.wa_id) is True
    assert await users_db.get_by_wa_id(conn, ana.wa_id) is None


async def test_wa_id_is_unique(conn, ana):
    import psycopg
    import pytest

    with pytest.raises(psycopg.errors.UniqueViolation):
        await users_db.create_user(conn, name="Impostor", wa_id=ana.wa_id)
```

- [ ] **Step 3: Run it and watch it fail**

Run: `pytest tests/test_users.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia.core.db.users'`

- [ ] **Step 4: Write `gaia/core/db/users.py`**

```python
from gaia.core.models import User

_COLUMNS = "id, name, wa_id, role, timezone, active"


def _row_to_user(row: dict) -> User:
    return User(
        id=row["id"],
        name=row["name"],
        wa_id=row["wa_id"],
        role=row["role"],
        timezone=row["timezone"],
        active=row["active"],
    )


async def create_user(
    conn,
    *,
    name: str,
    wa_id: str,
    role: str = "agent",
    timezone: str = "America/New_York",
) -> User:
    cur = await conn.execute(
        f"""INSERT INTO users (name, wa_id, role, timezone)
            VALUES (%s, %s, %s, %s) RETURNING {_COLUMNS}""",
        (name, wa_id, role, timezone),
    )
    return _row_to_user(await cur.fetchone())


async def get_by_wa_id(conn, wa_id: str) -> User | None:
    """Resolve an inbound number. Inactive users resolve to None, so
    deactivation is immediate revocation."""
    cur = await conn.execute(
        f"SELECT {_COLUMNS} FROM users WHERE wa_id = %s AND active", (wa_id,)
    )
    row = await cur.fetchone()
    return _row_to_user(row) if row else None


async def list_users(conn) -> list[User]:
    cur = await conn.execute(f"SELECT {_COLUMNS} FROM users ORDER BY created_at")
    return [_row_to_user(r) for r in await cur.fetchall()]


async def deactivate(conn, wa_id: str) -> bool:
    cur = await conn.execute(
        "UPDATE users SET active = false WHERE wa_id = %s RETURNING id", (wa_id,)
    )
    return await cur.fetchone() is not None


async def touch_inbound(conn, user: User) -> None:
    """Record that this user just messaged us — drives the 24h window check."""
    await conn.execute("UPDATE users SET last_inbound_at = now() WHERE id = %s", (user.id,))
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_users.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Write the admin CLI test**

`tests/test_admin.py`:

```python
from gaia.core.admin import build_parser


def test_parser_accepts_add_user():
    args = build_parser().parse_args(
        ["add-user", "--name", "Ana", "--phone", "13055550001", "--role", "admin"]
    )
    assert args.command == "add-user"
    assert args.name == "Ana"
    assert args.phone == "13055550001"
    assert args.role == "admin"


def test_parser_defaults_role_to_agent():
    args = build_parser().parse_args(["add-user", "--name", "Ana", "--phone", "1305"])
    assert args.role == "agent"


def test_parser_accepts_deactivate():
    args = build_parser().parse_args(["deactivate", "--phone", "13055550001"])
    assert args.command == "deactivate"
```

- [ ] **Step 7: Write `gaia/core/admin.py`**

```python
"""Roster management. The bootstrap: until one row exists in users, every
inbound number is ignored, so this cannot be replaced by an in-band flow."""

import argparse
import asyncio

from gaia.core.db import users as users_db
from gaia.core.db.pool import get_pool, tx


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m gaia.core.admin")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add-user")
    add.add_argument("--name", required=True)
    add.add_argument("--phone", required=True, help="country code, no '+'")
    add.add_argument("--role", default="agent", choices=["agent", "admin"])
    add.add_argument("--tz", default="America/New_York")

    sub.add_parser("list-users")

    off = sub.add_parser("deactivate")
    off.add_argument("--phone", required=True)

    return parser


async def _run(args) -> None:
    pool = get_pool()
    await pool.open(wait=True)
    try:
        async with tx(pool) as conn:
            if args.command == "add-user":
                user = await users_db.create_user(
                    conn, name=args.name, wa_id=args.phone, role=args.role, timezone=args.tz
                )
                print(f"added {user.name} ({user.wa_id}) as {user.role}")
            elif args.command == "list-users":
                for u in await users_db.list_users(conn):
                    state = "active" if u.active else "inactive"
                    print(f"{u.wa_id:<15} {u.name:<20} {u.role:<8} {state}")
            elif args.command == "deactivate":
                ok = await users_db.deactivate(conn, args.phone)
                print("deactivated" if ok else f"no user with phone {args.phone}")
    finally:
        await pool.close()


def main() -> None:
    asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 8: Run the full suite**

Run: `pytest -v`
Expected: PASS (all tests)

- [ ] **Step 9: Commit**

```bash
git add gaia/core/db/users.py gaia/core/admin.py tests
git commit -m "feat: users repository and admin CLI

get_by_wa_id filters on active, so deactivation is immediate revocation —
the response to a lost phone, which is the standing risk when a phone
number is the credential.

The CLI is the bootstrap: until one row exists in users every number is
ignored, so no in-band enrolment flow can replace it."
```

---

### Task 5: Contacts, and the isolation harness

This task establishes the isolation test pattern every later data task reuses.

**Files:**
- Create: `gaia/core/db/contacts.py`
- Create: `tests/test_isolation.py`, `tests/test_contacts.py`
- Create: `tests/factories.py`

**Interfaces:**
- Consumes: `visible`, `DOMAIN_TABLES`, `User`.
- Produces:
  - `create_contact(conn, user, *, name, visibility="org") -> UUID`
  - `get_or_create(conn, user, name: str) -> UUID` — reuses a visible contact with that name.
  - `roster(conn, user, limit=40) -> list[str]` — names only, most recently touched first.
  - `lookup(conn, user, name: str) -> dict | None` — full profile, scoped.
  - `merge_profile(conn, user, contact_id, update: str) -> None`
  - `tests.factories.make_row(conn, table, user, visibility) -> UUID` — inserts a minimal valid row into any domain table.

- [ ] **Step 1: Write the failing isolation test**

`tests/test_isolation.py`:

```python
import pytest

from gaia.core.db.scope import DOMAIN_TABLES, visible
from tests.factories import make_row


@pytest.mark.parametrize("table", DOMAIN_TABLES)
async def test_private_rows_are_invisible_to_other_users(conn, ana, sofia, table):
    """Ana files something private; Sofia must not be able to read it."""
    row_id = await make_row(conn, table, ana, visibility="private")

    cur = await conn.execute(
        f"SELECT id FROM {table} t WHERE t.id = %(id)s AND {visible('t')}",
        {"id": row_id, "scope_user_id": sofia.id},
    )
    assert await cur.fetchone() is None, f"{table}: Sofia can read Ana's private row"


@pytest.mark.parametrize("table", DOMAIN_TABLES)
async def test_org_rows_are_visible_to_other_users(conn, ana, sofia, table):
    row_id = await make_row(conn, table, ana, visibility="org")

    cur = await conn.execute(
        f"SELECT id FROM {table} t WHERE t.id = %(id)s AND {visible('t')}",
        {"id": row_id, "scope_user_id": sofia.id},
    )
    assert await cur.fetchone() is not None, f"{table}: org row hidden from Sofia"


@pytest.mark.parametrize("table", DOMAIN_TABLES)
async def test_owners_always_see_their_own_private_rows(conn, ana, table):
    row_id = await make_row(conn, table, ana, visibility="private")

    cur = await conn.execute(
        f"SELECT id FROM {table} t WHERE t.id = %(id)s AND {visible('t')}",
        {"id": row_id, "scope_user_id": ana.id},
    )
    assert await cur.fetchone() is not None
```

- [ ] **Step 2: Write `tests/factories.py`**

```python
"""Minimal valid rows for every domain table, so isolation tests can be
parametrized rather than hand-written per table."""

from uuid import UUID


async def make_row(conn, table: str, user, visibility: str = "org") -> UUID:
    if table == "contacts":
        return await _one(
            conn,
            "INSERT INTO contacts (user_id, visibility, name) VALUES (%s,%s,%s) RETURNING id",
            (user.id, visibility, "Delgado"),
        )
    if table == "meetings":
        return await _one(
            conn,
            """INSERT INTO meetings (user_id, visibility, source, summary)
               VALUES (%s,%s,'text',%s) RETURNING id""",
            (user.id, visibility, "Showing at Coral Gables"),
        )
    if table == "leads":
        contact_id = await make_row(conn, "contacts", user, visibility)
        return await _one(
            conn,
            """INSERT INTO leads (user_id, visibility, contact_id, description)
               VALUES (%s,%s,%s,%s) RETURNING id""",
            (user.id, visibility, contact_id, "Buying, ~600k"),
        )
    if table == "commitments":
        return await _one(
            conn,
            """INSERT INTO commitments (user_id, visibility, description)
               VALUES (%s,%s,%s) RETURNING id""",
            (user.id, visibility, "Send the listing update"),
        )
    if table == "memory_chunks":
        return await _one(
            conn,
            """INSERT INTO memory_chunks (user_id, visibility, content, embedding)
               VALUES (%s,%s,%s,%s) RETURNING id""",
            (user.id, visibility, "They liked the kitchen", [0.0] * 1024),
        )
    raise AssertionError(f"tests/factories.py has no builder for {table!r}")


async def _one(conn, sql: str, params: tuple) -> UUID:
    cur = await conn.execute(sql, params)
    return (await cur.fetchone())["id"]
```

> The `raise AssertionError` is the point: adding a domain table without a
> factory fails the parametrized suite loudly rather than skipping it.

- [ ] **Step 3: Run it and watch it fail**

Run: `pytest tests/test_isolation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.factories'` on the first run, then PASS once the factory exists. The schema from Task 3 already satisfies these assertions; this suite is the regression net for every later task.

- [ ] **Step 4: Write the contacts test**

`tests/test_contacts.py`:

```python
from gaia.core.db import contacts as contacts_db


async def test_get_or_create_reuses_an_existing_visible_contact(conn, ana):
    first = await contacts_db.get_or_create(conn, ana, "Maria Delgado")
    second = await contacts_db.get_or_create(conn, ana, "maria delgado")
    assert first == second


async def test_get_or_create_does_not_reuse_another_users_private_contact(conn, ana, sofia):
    hidden = await contacts_db.create_contact(
        conn, ana, name="Maria Delgado", visibility="private"
    )
    mine = await contacts_db.get_or_create(conn, sofia, "Maria Delgado")
    assert mine != hidden


async def test_roster_returns_names_not_profiles(conn, ana):
    cid = await contacts_db.get_or_create(conn, ana, "Maria Delgado")
    await contacts_db.merge_profile(conn, ana, cid, "wants a pool")
    names = await contacts_db.roster(conn, ana)
    assert names == ["Maria Delgado"]


async def test_lookup_returns_the_profile(conn, ana):
    cid = await contacts_db.get_or_create(conn, ana, "Maria Delgado")
    await contacts_db.merge_profile(conn, ana, cid, "wants a pool")
    await contacts_db.merge_profile(conn, ana, cid, "budget 600k")
    found = await contacts_db.lookup(conn, ana, "Maria Delgado")
    assert found is not None
    assert "wants a pool" in found["profile"]
    assert "budget 600k" in found["profile"]


async def test_lookup_cannot_read_another_users_private_contact(conn, ana, sofia):
    cid = await contacts_db.create_contact(
        conn, ana, name="Maria Delgado", visibility="private"
    )
    await contacts_db.merge_profile(conn, ana, cid, "wants a pool")
    assert await contacts_db.lookup(conn, sofia, "Maria Delgado") is None


async def test_profile_is_capped(conn, ana):
    cid = await contacts_db.get_or_create(conn, ana, "Maria Delgado")
    for i in range(400):
        await contacts_db.merge_profile(conn, ana, cid, f"fact number {i}")
    found = await contacts_db.lookup(conn, ana, "Maria Delgado")
    assert len(found["profile"]) <= contacts_db.PROFILE_CAP
```

- [ ] **Step 5: Write `gaia/core/db/contacts.py`**

```python
from uuid import UUID

from gaia.core.db.scope import visible
from gaia.core.models import User

PROFILE_CAP = 4000  # characters; consolidation is deferred, the cap is not


async def create_contact(conn, user: User, *, name: str, visibility: str = "org") -> UUID:
    cur = await conn.execute(
        "INSERT INTO contacts (user_id, visibility, name) VALUES (%s,%s,%s) RETURNING id",
        (user.id, visibility, name),
    )
    return (await cur.fetchone())["id"]


async def get_or_create(conn, user: User, name: str) -> UUID:
    cur = await conn.execute(
        f"""SELECT id FROM contacts t
            WHERE lower(t.name) = lower(%(name)s) AND {visible('t')}
            ORDER BY t.updated_at DESC LIMIT 1""",
        {"name": name, "scope_user_id": user.id},
    )
    row = await cur.fetchone()
    return row["id"] if row else await create_contact(conn, user, name=name)


async def roster(conn, user: User, limit: int = 40) -> list[str]:
    """Names only. Full profiles are fetched on demand via lookup(), so the
    request does not grow with the company's whole contact book."""
    cur = await conn.execute(
        f"""SELECT t.name FROM contacts t
            WHERE {visible('t')}
            ORDER BY t.updated_at DESC LIMIT %(limit)s""",
        {"scope_user_id": user.id, "limit": limit},
    )
    return [r["name"] for r in await cur.fetchall()]


async def lookup(conn, user: User, name: str) -> dict | None:
    cur = await conn.execute(
        f"""SELECT t.name, t.profile, t.phone, t.email FROM contacts t
            WHERE lower(t.name) = lower(%(name)s) AND {visible('t')}
            ORDER BY t.updated_at DESC LIMIT 1""",
        {"name": name, "scope_user_id": user.id},
    )
    return await cur.fetchone()


async def merge_profile(conn, user: User, contact_id: UUID, update: str) -> None:
    """Append a fact, then trim from the front if the profile exceeds the cap.

    Append-only accretion is inherited from the prototype; the cap keeps it
    bounded until a consolidation pass rewrites profiles properly.
    """
    await conn.execute(
        """UPDATE contacts
           SET profile = CASE WHEN profile = '' THEN %(u)s ELSE profile || ' | ' || %(u)s END,
               updated_at = now()
           WHERE id = %(id)s""",
        {"u": update, "id": contact_id},
    )
    await conn.execute(
        """UPDATE contacts SET profile = right(profile, %(cap)s)
           WHERE id = %(id)s AND length(profile) > %(cap)s""",
        {"cap": PROFILE_CAP, "id": contact_id},
    )
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_isolation.py tests/test_contacts.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add gaia/core/db/contacts.py tests
git commit -m "feat: contacts repository and parametrized isolation suite

Isolation tests are parametrized over DOMAIN_TABLES with a factory per
table, so a new domain table is covered by construction — hand-listed
tests would silently leave the next table uncovered, and a missing
isolation test looks exactly like a passing one.

Contacts expose a names-only roster plus an on-demand lookup, replacing
the prototype's inject-every-profile-every-request approach, which was
sound for one user and unbounded once contacts are org-shared."
```

---

### Task 6: Meetings, commitments, and inherited visibility

**Files:**
- Create: `gaia/core/db/meetings.py`, `gaia/core/db/commitments.py`
- Create: `tests/test_meetings.py`

**Interfaces:**
- Consumes: `contacts.get_or_create`, `contacts.merge_profile`.
- Produces:
  - `meetings.save(conn, user, *, summary, source, raw_input=None, happened_at=None, visibility="org", contact_names=(), commitments=()) -> UUID`
  - `meetings.set_visibility(conn, user, meeting_id, visibility) -> None`
  - `commitments.open_for(conn, user, within_days=2) -> list[dict]` — **ownership-scoped**.
  - `commitments.complete(conn, user, commitment_id) -> bool`
  - `commitments.mark_nudged(conn, user, ids: list[UUID]) -> None`

- [ ] **Step 1: Write the failing test**

`tests/test_meetings.py`:

```python
from datetime import datetime, timedelta, timezone

from gaia.core.db import commitments as commitments_db
from gaia.core.db import meetings as meetings_db


async def test_save_creates_contacts_and_commitments(conn, ana):
    mid = await meetings_db.save(
        conn,
        ana,
        summary="Showed Coral Gables to the Delgados",
        source="text",
        contact_names=["Maria Delgado"],
        commitments=[{"description": "Send comps", "contact_name": "Maria Delgado"}],
    )
    assert mid is not None
    open_items = await commitments_db.open_for(conn, ana, within_days=365)
    assert [c["description"] for c in open_items] == ["Send comps"]


async def test_happened_at_can_differ_from_now(conn, ana):
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    mid = await meetings_db.save(
        conn, ana, summary="Yesterday's showing", source="photo_notes", happened_at=yesterday
    )
    cur = await conn.execute("SELECT happened_at FROM meetings WHERE id = %s", (mid,))
    assert (await cur.fetchone())["happened_at"].date() == yesterday.date()


async def test_derived_rows_inherit_visibility_on_insert(conn, ana):
    mid = await meetings_db.save(
        conn,
        ana,
        summary="Sensitive divorce sale",
        source="text",
        visibility="private",
        commitments=[{"description": "Call the attorney"}],
    )
    cur = await conn.execute(
        "SELECT visibility FROM commitments WHERE meeting_id = %s", (mid,)
    )
    assert (await cur.fetchone())["visibility"] == "private"


async def test_flipping_a_meeting_to_private_cascades(conn, ana):
    mid = await meetings_db.save(
        conn,
        ana,
        summary="Routine showing",
        source="text",
        commitments=[{"description": "Send comps"}],
    )
    await meetings_db.set_visibility(conn, ana, mid, "private")

    cur = await conn.execute(
        "SELECT visibility FROM commitments WHERE meeting_id = %s", (mid,)
    )
    assert (await cur.fetchone())["visibility"] == "private"


async def test_open_commitments_are_scoped_by_ownership_not_visibility(conn, ana, sofia):
    """Sofia's org-visible commitment is readable by Ana, but is not her work."""
    await meetings_db.save(
        conn, sofia, summary="Sofia's meeting", source="text",
        commitments=[{"description": "Sofia's task"}],
    )
    assert await commitments_db.open_for(conn, ana, within_days=365) == []
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_meetings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia.core.db.meetings'`

- [ ] **Step 3: Write `gaia/core/db/meetings.py`**

```python
from datetime import datetime
from uuid import UUID

from gaia.core.db import contacts as contacts_db
from gaia.core.db.scope import visible
from gaia.core.models import User


async def save(
    conn,
    user: User,
    *,
    summary: str,
    source: str,
    raw_input: str | None = None,
    happened_at: datetime | None = None,
    visibility: str = "org",
    contact_names: tuple[str, ...] = (),
    commitments: tuple[dict, ...] = (),
) -> UUID:
    """Create a meeting and everything derived from it, in one transaction.

    Derived rows copy the meeting's visibility and owner. They never take
    their own — a private note whose embedding defaults to org-visible is
    searchable by the whole company.
    """
    cur = await conn.execute(
        """INSERT INTO meetings (user_id, visibility, source, raw_input, summary, happened_at)
           VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now())) RETURNING id""",
        (user.id, visibility, source, raw_input, summary, happened_at),
    )
    meeting_id = (await cur.fetchone())["id"]

    contact_ids: dict[str, UUID] = {}
    for name in contact_names:
        cid = await contacts_db.get_or_create(conn, user, name)
        contact_ids[name.lower()] = cid
        await conn.execute(
            """INSERT INTO meeting_contacts (meeting_id, contact_id) VALUES (%s, %s)
               ON CONFLICT DO NOTHING""",
            (meeting_id, cid),
        )

    for item in commitments:
        cid = contact_ids.get((item.get("contact_name") or "").lower())
        await conn.execute(
            """INSERT INTO commitments
                   (user_id, visibility, meeting_id, contact_id, description, due_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (user.id, visibility, meeting_id, cid, item["description"], item.get("due_at")),
        )

    return meeting_id


async def set_visibility(conn, user: User, meeting_id: UUID, visibility: str) -> None:
    """Reclassify. The trg_cascade_meeting_visibility trigger propagates to
    memory_chunks and commitments."""
    await conn.execute(
        "UPDATE meetings SET visibility = %s WHERE id = %s AND user_id = %s",
        (visibility, meeting_id, user.id),
    )


async def recent(conn, user: User, limit: int = 10) -> list[dict]:
    cur = await conn.execute(
        f"""SELECT t.id, t.summary, t.happened_at FROM meetings t
            WHERE {visible('t')} ORDER BY t.happened_at DESC LIMIT %(limit)s""",
        {"scope_user_id": user.id, "limit": limit},
    )
    return await cur.fetchall()
```

- [ ] **Step 4: Write `gaia/core/db/commitments.py`**

```python
from uuid import UUID

from gaia.core.models import User


async def open_for(conn, user: User, within_days: int = 2) -> list[dict]:
    """Commitments this user owes. Scoped by OWNERSHIP, not visibility —
    a colleague's org-visible task is readable but is not this user's work."""
    cur = await conn.execute(
        """SELECT c.id, c.description, c.due_at, c.nudge_count, ct.name AS contact
           FROM commitments c
           LEFT JOIN contacts ct ON ct.id = c.contact_id
           WHERE c.user_id = %(uid)s
             AND c.done_at IS NULL
             AND (c.due_at IS NULL OR c.due_at <= now() + make_interval(days => %(days)s))
           ORDER BY c.due_at NULLS LAST""",
        {"uid": user.id, "days": within_days},
    )
    return await cur.fetchall()


async def complete(conn, user: User, commitment_id: UUID) -> bool:
    cur = await conn.execute(
        """UPDATE commitments SET done_at = now()
           WHERE id = %s AND user_id = %s AND done_at IS NULL RETURNING id""",
        (commitment_id, user.id),
    )
    return await cur.fetchone() is not None


async def mark_nudged(conn, user: User, ids: list[UUID]) -> None:
    if not ids:
        return
    await conn.execute(
        """UPDATE commitments
           SET nudge_count = nudge_count + 1, last_nudged_at = now()
           WHERE id = ANY(%s) AND user_id = %s""",
        (ids, user.id),
    )
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_meetings.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add gaia/core/db/meetings.py gaia/core/db/commitments.py tests/test_meetings.py
git commit -m "feat: meetings and commitments with inherited visibility

Derived rows copy the meeting's visibility and owner on insert; the schema
trigger handles reclassification afterwards. Both paths are tested.

happened_at is supplied by the caller rather than always now(), so notes
photographed the next morning file under the day the meeting happened.

open_for() filters on ownership, not visibility — the distinction that
keeps the morning digest from nagging people about colleagues' work."
```

---

### Task 7: Semantic memory

**Files:**
- Create: `gaia/core/db/memory.py`, `gaia/core/embeddings.py`
- Create: `tests/test_memory.py`
- Modify: `tests/conftest.py` (add a `fake_embed` autouse fixture)

**Interfaces:**
- Consumes: `meetings.save`, `visible`.
- Produces:
  - `gaia.core.embeddings.embed(texts: list[str], input_type: str) -> list[list[float]]` — async, wraps the sync Voyage client in a thread.
  - `memory.index_meeting(conn, user, meeting_id, content, visibility, contact_id=None) -> None`
  - `memory.search(conn, user, query: str, contact_name: str | None = None, limit: int = 6) -> list[dict]`

- [ ] **Step 1: Add the embedding stub to `tests/conftest.py`**

```python
import pytest


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch):
    """Deterministic embeddings. Tests assert on scoping, not on similarity —
    real Voyage calls would make the suite slow, flaky and billable."""
    async def _embed(texts, input_type="document"):
        return [[float(len(t) % 7)] + [0.0] * 1023 for t in texts]

    monkeypatch.setattr("gaia.core.embeddings.embed", _embed)
    monkeypatch.setattr("gaia.core.db.memory.embed", _embed)
```

- [ ] **Step 2: Write the failing test**

`tests/test_memory.py`:

```python
from gaia.core.db import meetings as meetings_db
from gaia.core.db import memory as memory_db


async def _meeting_with_chunk(conn, user, summary, visibility="org"):
    mid = await meetings_db.save(conn, user, summary=summary, source="text", visibility=visibility)
    await memory_db.index_meeting(conn, user, mid, summary, visibility)
    return mid


async def test_search_finds_an_org_chunk_from_another_user(conn, ana, sofia):
    await _meeting_with_chunk(conn, ana, "Delgados liked the kitchen")
    hits = await memory_db.search(conn, sofia, "kitchen")
    assert any("kitchen" in h["content"] for h in hits)


async def test_search_excludes_another_users_private_chunk(conn, ana, sofia):
    await _meeting_with_chunk(conn, ana, "Divorce sale, motivated", visibility="private")
    hits = await memory_db.search(conn, sofia, "divorce")
    assert hits == []


async def test_owner_can_search_their_own_private_chunk(conn, ana):
    await _meeting_with_chunk(conn, ana, "Divorce sale, motivated", visibility="private")
    hits = await memory_db.search(conn, ana, "divorce")
    assert len(hits) == 1


async def test_flipping_a_meeting_private_removes_it_from_others_search(conn, ana, sofia):
    mid = await _meeting_with_chunk(conn, ana, "Routine showing notes")
    assert await memory_db.search(conn, sofia, "showing") != []

    await meetings_db.set_visibility(conn, ana, mid, "private")
    assert await memory_db.search(conn, sofia, "showing") == []
```

- [ ] **Step 3: Run it and watch it fail**

Run: `pytest tests/test_memory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia.core.db.memory'`

- [ ] **Step 4: Write `gaia/core/embeddings.py`**

```python
import asyncio

import voyageai

from gaia.core.config import settings

MODEL = "voyage-3.5-lite"  # 1024 dims, matching vector(1024) in the schema

_client = voyageai.Client(api_key=settings.voyage_api_key)


async def embed(texts: list[str], input_type: str = "document") -> list[list[float]]:
    """The Voyage client is synchronous; keep it off the event loop."""
    result = await asyncio.to_thread(
        _client.embed, texts, model=MODEL, input_type=input_type
    )
    return result.embeddings
```

- [ ] **Step 5: Write `gaia/core/db/memory.py`**

```python
from uuid import UUID

from gaia.core.embeddings import embed
from gaia.core.db.scope import visible
from gaia.core.models import User


async def index_meeting(
    conn,
    user: User,
    meeting_id: UUID,
    content: str,
    visibility: str,
    contact_id: UUID | None = None,
) -> None:
    """Visibility is passed in from the parent meeting, never defaulted."""
    vector = (await embed([content], input_type="document"))[0]
    await conn.execute(
        """INSERT INTO memory_chunks
               (user_id, visibility, meeting_id, contact_id, content, embedding)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (user.id, visibility, meeting_id, contact_id, content, vector),
    )


async def search(
    conn, user: User, query: str, contact_name: str | None = None, limit: int = 6
) -> list[dict]:
    vector = (await embed([query], input_type="query"))[0]

    name_clause = ""
    params: dict = {"emb": vector, "scope_user_id": user.id, "limit": limit}
    if contact_name:
        name_clause = "AND lower(ct.name) = lower(%(name)s)"
        params["name"] = contact_name

    cur = await conn.execute(
        f"""SELECT t.content, t.created_at, ct.name AS contact
            FROM memory_chunks t
            LEFT JOIN contacts ct ON ct.id = t.contact_id
            WHERE {visible('t')} {name_clause}
            ORDER BY t.embedding <=> %(emb)s::vector
            LIMIT %(limit)s""",
        params,
    )
    return [
        {
            "content": r["content"],
            "date": r["created_at"].date().isoformat(),
            "contact": r["contact"],
        }
        for r in await cur.fetchall()
    ]
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_memory.py -v`
Expected: PASS (4 tests)

- [ ] **Step 7: Commit**

```bash
git add gaia/core/embeddings.py gaia/core/db/memory.py tests
git commit -m "feat: scoped semantic memory

Vector search applies the visibility fragment, and index_meeting takes
visibility from its parent rather than defaulting. Without both, a private
meeting stays hidden from the meetings list while remaining fully
searchable by the whole company.

The Voyage client is synchronous and now runs in a thread rather than
blocking the event loop."
```

---

### Task 8: Leads and the message thread

**Files:**
- Create: `gaia/core/db/leads.py`, `gaia/core/db/messages.py`
- Create: `tests/test_leads.py`, `tests/test_messages.py`, `tests/test_db_signatures.py`

**Interfaces:**
- Produces:
  - `leads.create(conn, user, *, contact_name, description, status="new", next_action_at=None, next_action_note=None, visibility="org") -> UUID`
  - `leads.due_for(conn, user) -> list[dict]` — **ownership-scoped**.
  - `leads.query(conn, user, *, due_only=False, status=None) -> list[dict]` — visibility-scoped.
  - `leads.update(conn, user, lead_id, **fields) -> bool`
  - `leads.mark_nudged(conn, user, ids) -> None`
  - `messages.log(conn, user, role, content, wa_msg_id=None) -> None`
  - `messages.seen(conn, wa_msg_id) -> bool`
  - `messages.recent(conn, user, limit=20, exclude_wa_ids=()) -> list[dict]`

- [ ] **Step 1: Write the failing lead test**

`tests/test_leads.py`:

```python
from datetime import datetime, timedelta, timezone

from gaia.core.db import leads as leads_db


async def test_a_lead_can_be_created_and_read_back(conn, ana):
    lead_id = await leads_db.create(
        conn, ana, contact_name="Maria Delgado", description="Buying in Coral Gables, ~600k"
    )
    rows = await leads_db.query(conn, ana)
    assert [r["id"] for r in rows] == [lead_id]
    assert rows[0]["name"] == "Maria Delgado"


async def test_due_leads_are_scoped_by_ownership(conn, ana, sofia):
    """Sofia's due lead is org-visible, and still must not appear in Ana's."""
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await leads_db.create(
        conn, sofia, contact_name="Rivera", description="Selling", next_action_at=past
    )
    assert await leads_db.due_for(conn, ana) == []
    assert len(await leads_db.due_for(conn, sofia)) == 1


async def test_query_is_scoped_by_visibility(conn, ana, sofia):
    await leads_db.create(
        conn, ana, contact_name="Private Client", description="Quiet sale",
        visibility="private",
    )
    assert await leads_db.query(conn, sofia) == []
    assert len(await leads_db.query(conn, ana)) == 1


async def test_update_changes_status_and_next_action(conn, ana):
    lead_id = await leads_db.create(
        conn, ana, contact_name="Maria Delgado", description="Buying"
    )
    when = datetime.now(timezone.utc) + timedelta(days=3)
    assert await leads_db.update(
        conn, ana, lead_id, status="active", next_action_at=when,
        next_action_note="Send Friday listings",
    ) is True

    row = (await leads_db.query(conn, ana))[0]
    assert row["status"] == "active"
    assert row["next_action_note"] == "Send Friday listings"


async def test_update_cannot_touch_another_users_lead(conn, ana, sofia):
    lead_id = await leads_db.create(conn, ana, contact_name="Maria", description="Buying")
    assert await leads_db.update(conn, sofia, lead_id, status="lost") is False
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_leads.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia.core.db.leads'`

- [ ] **Step 3: Write `gaia/core/db/leads.py`**

```python
from uuid import UUID

from gaia.core.db import contacts as contacts_db
from gaia.core.db.scope import visible
from gaia.core.models import User

_SELECT = """SELECT l.id, ct.name, l.description, l.status, l.next_action_at,
                    l.next_action_note, l.nudge_count
             FROM leads l JOIN contacts ct ON ct.id = l.contact_id"""

_UPDATABLE = ("status", "next_action_at", "next_action_note", "description")


async def create(
    conn,
    user: User,
    *,
    contact_name: str,
    description: str,
    status: str = "new",
    next_action_at=None,
    next_action_note: str | None = None,
    visibility: str = "org",
) -> UUID:
    """The prototype could read and update leads but never insert one, so the
    pipeline was permanently empty and the digest could only ever see
    commitments. This is that missing half."""
    contact_id = await contacts_db.get_or_create(conn, user, contact_name)
    cur = await conn.execute(
        """INSERT INTO leads (user_id, visibility, contact_id, description, status,
                              next_action_at, next_action_note)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (user.id, visibility, contact_id, description, status,
         next_action_at, next_action_note),
    )
    return (await cur.fetchone())["id"]


async def due_for(conn, user: User) -> list[dict]:
    """Leads this user must act on. Ownership-scoped — see spec §3.3."""
    cur = await conn.execute(
        f"""{_SELECT}
            WHERE l.user_id = %(uid)s
              AND l.next_action_at IS NOT NULL AND l.next_action_at <= now()
              AND l.status IN ('new','active')
            ORDER BY l.next_action_at""",
        {"uid": user.id},
    )
    return await cur.fetchall()


async def query(conn, user: User, *, due_only: bool = False, status: str | None = None) -> list[dict]:
    """Answering a question, so visibility-scoped rather than ownership-scoped."""
    clauses = [visible("l")]
    params: dict = {"scope_user_id": user.id}
    if due_only:
        clauses.append("l.next_action_at <= now()")
    if status:
        clauses.append("l.status = %(status)s")
        params["status"] = status

    cur = await conn.execute(
        f"{_SELECT} WHERE {' AND '.join(clauses)} ORDER BY l.next_action_at NULLS LAST LIMIT 25",
        params,
    )
    return await cur.fetchall()


async def update(conn, user: User, lead_id: UUID, **fields) -> bool:
    sets, params = ["updated_at = now()"], {"id": lead_id, "uid": user.id}
    for key in _UPDATABLE:
        if fields.get(key) is not None:
            sets.append(f"{key} = %({key})s")
            params[key] = fields[key]

    cur = await conn.execute(
        f"UPDATE leads SET {', '.join(sets)} WHERE id = %(id)s AND user_id = %(uid)s RETURNING id",
        params,
    )
    return await cur.fetchone() is not None


async def mark_nudged(conn, user: User, ids: list[UUID]) -> None:
    if not ids:
        return
    await conn.execute(
        """UPDATE leads SET nudge_count = nudge_count + 1, last_nudged_at = now()
           WHERE id = ANY(%s) AND user_id = %s""",
        (ids, user.id),
    )
```

- [ ] **Step 4: Write the message thread test**

`tests/test_messages.py`:

```python
from gaia.core.db import messages as messages_db


async def test_thread_is_private_to_its_owner(conn, ana, sofia):
    await messages_db.log(conn, ana, "user", "Notes from the Delgado showing", "wamid.1")
    assert await messages_db.recent(conn, sofia) == []
    assert len(await messages_db.recent(conn, ana)) == 1


async def test_duplicate_wa_msg_id_is_ignored(conn, ana):
    await messages_db.log(conn, ana, "user", "hello", "wamid.1")
    assert await messages_db.seen(conn, "wamid.1") is True
    await messages_db.log(conn, ana, "user", "hello", "wamid.1")
    assert len(await messages_db.recent(conn, ana)) == 1


async def test_recent_excludes_the_current_message(conn, ana):
    """The prototype logged the inbound message then read it straight back as
    history, sending it twice — once as text without its image."""
    await messages_db.log(conn, ana, "user", "older", "wamid.1")
    await messages_db.log(conn, ana, "user", "current", "wamid.2")
    history = await messages_db.recent(conn, ana, exclude_wa_ids=["wamid.2"])
    assert [m["content"] for m in history] == ["older"]


async def test_recent_returns_oldest_first(conn, ana):
    await messages_db.log(conn, ana, "user", "first", "wamid.1")
    await messages_db.log(conn, ana, "assistant", "second", None)
    assert [m["content"] for m in await messages_db.recent(conn, ana)] == ["first", "second"]
```

- [ ] **Step 5: Write `gaia/core/db/messages.py`**

```python
from gaia.core.models import User


async def log(conn, user: User, role: str, content: str, wa_msg_id: str | None = None) -> None:
    await conn.execute(
        """INSERT INTO messages (user_id, role, content, wa_msg_id)
           VALUES (%s,%s,%s,%s) ON CONFLICT (wa_msg_id) DO NOTHING""",
        (user.id, role, content, wa_msg_id),
    )


async def seen(conn, wa_msg_id: str) -> bool:
    """Dedup is roster-independent: WhatsApp redelivers regardless of who sent it."""
    cur = await conn.execute("SELECT 1 FROM messages WHERE wa_msg_id = %s", (wa_msg_id,))
    return await cur.fetchone() is not None


async def recent(conn, user: User, limit: int = 20, exclude_wa_ids: tuple[str, ...] = ()) -> list[dict]:
    """Oldest first. A thread is always owner-only — no visibility fragment."""
    cur = await conn.execute(
        """SELECT role, content FROM (
               SELECT role, content, created_at FROM messages
               WHERE user_id = %(uid)s
                 AND (%(excluded)s::text[] IS NULL OR NOT (wa_msg_id = ANY(%(excluded)s)))
               ORDER BY created_at DESC LIMIT %(limit)s
           ) recent ORDER BY created_at ASC""",
        {"uid": user.id, "limit": limit, "excluded": list(exclude_wa_ids) or None},
    )
    return await cur.fetchall()
```

- [ ] **Step 6: Write the signature tripwire**

`tests/test_db_signatures.py`:

```python
import importlib
import inspect
import pkgutil

import gaia.core.db

# users.py manages the roster itself, so its functions take wa_id, not a User.
EXEMPT = {"gaia.core.db.users", "gaia.core.db.pool", "gaia.core.db.migrate", "gaia.core.db.scope"}


def test_every_db_function_takes_user_first():
    """A tripwire for carelessness, not a proof of correct scoping — the
    parametrized isolation suite is what establishes that."""
    offenders = []
    for mod in pkgutil.iter_modules(gaia.core.db.__path__):
        name = f"gaia.core.db.{mod.name}"
        if name in EXEMPT:
            continue
        module = importlib.import_module(name)
        for fn_name, fn in inspect.getmembers(module, inspect.isfunction):
            if fn_name.startswith("_") or fn.__module__ != name:
                continue
            params = list(inspect.signature(fn).parameters)
            if params[:2] != ["conn", "user"]:
                offenders.append(f"{name}.{fn_name}{tuple(params)}")
    assert not offenders, "must take (conn, user, ...): " + ", ".join(offenders)
```

- [ ] **Step 7: Run the full suite**

Run: `pytest -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add gaia/core/db/leads.py gaia/core/db/messages.py tests
git commit -m "feat: leads with creation, and the per-user message thread

leads.create is the half the prototype never had: it read and updated the
table but never inserted, so query_leads was permanently empty and the
digest could only ever see commitments.

due_for() is ownership-scoped and query() is visibility-scoped — the same
table answering two different questions.

recent() excludes the current message, fixing the prototype bug where the
inbound message was logged and then read straight back as history."
```

---

### Task 9: Capability registry

**Files:**
- Create: `gaia/capabilities/__init__.py`, `gaia/capabilities/base.py`
- Create: `tests/test_registry.py`

**Interfaces:**
- Produces:
  - `Tool` — frozen dataclass: `name: str`, `description: str`, `input_schema: dict`, `handler: Callable[[Any, User, dict], Awaitable[dict]]`.
  - `Capability` — frozen dataclass: `name`, `description`, `tools: tuple[Tool, ...]`, `prompt_fragment: str = ""`, `allowed_roles: frozenset[str] | None = None`, `allowed_user_ids: frozenset[UUID] | None = None`; method `visible_to(user) -> bool`.
  - `Registry.register(cap)`, `Registry.for_user(user) -> list[Capability]`, `Registry.tool_defs(user) -> list[dict]`, `Registry.dispatch(conn, user, name, args) -> str`.
  - Module-level `registry = Registry()`.

- [ ] **Step 1: Write the failing test**

`tests/test_registry.py`:

```python
import json

import pytest

from gaia.capabilities.base import Capability, Registry, Tool


async def _echo(conn, user, args):
    return {"echoed": args.get("value")}


PUBLIC = Capability(
    name="public_cap",
    description="Everyone",
    tools=(Tool("echo", "Echo a value", {"type": "object", "properties": {}}, _echo),),
)
ADMIN_ONLY = Capability(
    name="admin_cap",
    description="Admins only",
    tools=(Tool("secret", "Secret", {"type": "object", "properties": {}}, _echo),),
    allowed_roles=frozenset({"admin"}),
)


@pytest.fixture
def registry():
    r = Registry()
    r.register(PUBLIC)
    r.register(ADMIN_ONLY)
    return r


def test_public_capability_is_visible_to_everyone(registry, ana):
    assert [c.name for c in registry.for_user(ana)] == ["public_cap"]


def test_restricted_capability_is_absent_from_the_tool_list(registry, ana):
    assert [t["name"] for t in registry.tool_defs(ana)] == ["echo"]


def test_restricted_capability_is_present_for_an_allowed_role(registry, ana):
    admin = type(ana)(**{**ana.__dict__, "role": "admin"})
    assert {t["name"] for t in registry.tool_defs(admin)} == {"echo", "secret"}


async def test_dispatch_refuses_a_tool_from_an_invisible_capability(registry, ana):
    result = await registry.dispatch(None, ana, "secret", {})
    assert "not available" in result


async def test_dispatch_runs_a_visible_tool(registry, ana):
    result = await registry.dispatch(None, ana, "echo", {"value": 42})
    assert json.loads(result) == {"echoed": 42}


async def test_dispatch_reports_an_unknown_tool(registry, ana):
    assert "unknown tool" in await registry.dispatch(None, ana, "nope", {})
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia.capabilities'`

- [ ] **Step 3: Write `gaia/capabilities/base.py`**

```python
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from gaia.core.models import User

log = logging.getLogger("gaia.registry")

Handler = Callable[[Any, User, dict], Awaitable[dict]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Handler

    def to_api(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    tools: tuple[Tool, ...]
    prompt_fragment: str = ""
    allowed_roles: frozenset[str] | None = None
    allowed_user_ids: frozenset[UUID] | None = None

    def visible_to(self, user: User) -> bool:
        """Public by default. A capability restricts itself; nothing else does."""
        if self.allowed_roles is not None and user.role in self.allowed_roles:
            return True
        if self.allowed_user_ids is not None and user.id in self.allowed_user_ids:
            return True
        return self.allowed_roles is None and self.allowed_user_ids is None


class Registry:
    def __init__(self) -> None:
        self._capabilities: list[Capability] = []

    def register(self, capability: Capability) -> None:
        self._capabilities.append(capability)

    def all(self) -> list[Capability]:
        return list(self._capabilities)

    def for_user(self, user: User) -> list[Capability]:
        return [c for c in self._capabilities if c.visible_to(user)]

    def tool_defs(self, user: User) -> list[dict]:
        return [t.to_api() for c in self.for_user(user) for t in c.tools]

    def prompt_fragments(self, user: User) -> str:
        return "".join(c.prompt_fragment for c in self.for_user(user))

    async def dispatch(self, conn, user: User, name: str, args: dict) -> str:
        """Re-checks visibility. Filtering the tool list is presentation; this
        is enforcement — a hallucinated tool name must not execute."""
        for capability in self._capabilities:
            for tool in capability.tools:
                if tool.name != name:
                    continue
                if not capability.visible_to(user):
                    log.warning("user %s attempted hidden tool %s", user.id, name)
                    return f"tool {name} is not available"
                try:
                    return json.dumps(await tool.handler(conn, user, args), default=str)
                except Exception as exc:  # surfaced to the model, not the user
                    log.exception("tool %s failed", name)
                    return f"tool error: {exc}"
        return f"unknown tool {name}"


registry = Registry()
```

Create `gaia/capabilities/__init__.py`:

```python
"""Capability registration. Importing this module populates the registry.

Adding a capability is: a directory here, a Capability instance, one import.
"""

from gaia.capabilities.base import registry

__all__ = ["registry"]
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_registry.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add gaia/capabilities tests/test_registry.py
git commit -m "feat: capability registry with per-user assembly

Capabilities are public by default and restrict themselves. Tools are
filtered out of a user's tool list AND re-checked at dispatch: the filter
is presentation, the dispatch check is enforcement."
```

---

### Task 10: Agent loop

**Files:**
- Create: `gaia/core/llm.py`
- Create: `tests/test_llm.py`, `tests/fakes.py`

**Interfaces:**
- Consumes: `registry`, `settings`.
- Produces:
  - `run_agent(client, conn, user, messages: list[dict], system: str, tool_defs: list[dict]) -> str`
  - `FALLBACK_TEXT: str` — what gets sent when the model returns nothing usable.

- [ ] **Step 1: Write the fakes**

`tests/fakes.py`:

```python
"""Scripted Anthropic and WhatsApp doubles."""

from dataclasses import dataclass, field


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class FakeResponse:
    content: list
    stop_reason: str = "end_turn"


class FakeAnthropic:
    """Returns queued responses in order and records the requests it received."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.requests: list[dict] = []
        self.messages = self

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeAnthropic ran out of scripted responses")
        return self._responses.pop(0)


class FakeWhatsApp:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.templates: list[tuple[str, str]] = []

    async def send_text(self, to: str, body: str) -> None:
        self.sent.append((to, body))

    async def send_template(self, to: str, body: str) -> None:
        self.templates.append((to, body))
```

- [ ] **Step 2: Write the failing test**

`tests/test_llm.py`:

```python
from gaia.capabilities.base import Capability, Registry, Tool
from gaia.core.llm import FALLBACK_TEXT, run_agent
from tests.fakes import FakeAnthropic, FakeResponse, TextBlock, ToolUseBlock


async def test_returns_text_when_the_model_stops(ana):
    client = FakeAnthropic([FakeResponse([TextBlock("Got it — filed.")])])
    out = await run_agent(client, None, ana, [{"role": "user", "content": "hi"}], "sys", [])
    assert out == "Got it — filed."


async def test_runs_a_tool_then_replies(ana):
    calls = []

    async def handler(conn, user, args):
        calls.append(args)
        return {"saved": True}

    reg = Registry()
    reg.register(
        Capability("c", "d", (Tool("save", "Save", {"type": "object", "properties": {}}, handler),))
    )

    client = FakeAnthropic([
        FakeResponse([ToolUseBlock("tu_1", "save", {"summary": "x"})], stop_reason="tool_use"),
        FakeResponse([TextBlock("Filed.")]),
    ])
    out = await run_agent(
        client, None, ana, [{"role": "user", "content": "notes"}], "sys", [], registry=reg
    )
    assert calls == [{"summary": "x"}]
    assert out == "Filed."


async def test_empty_response_falls_back(ana):
    client = FakeAnthropic([FakeResponse([])])
    out = await run_agent(client, None, ana, [{"role": "user", "content": "hi"}], "sys", [])
    assert out == FALLBACK_TEXT


async def test_refusal_falls_back(ana):
    client = FakeAnthropic([FakeResponse([], stop_reason="refusal")])
    out = await run_agent(client, None, ana, [{"role": "user", "content": "hi"}], "sys", [])
    assert out == FALLBACK_TEXT


async def test_iteration_cap_terminates(ana):
    async def handler(conn, user, args):
        return {"ok": True}

    reg = Registry()
    reg.register(
        Capability("c", "d", (Tool("loop", "Loop", {"type": "object", "properties": {}}, handler),))
    )
    client = FakeAnthropic(
        [FakeResponse([ToolUseBlock(f"tu_{i}", "loop", {})], stop_reason="tool_use")
         for i in range(20)]
    )
    out = await run_agent(
        client, None, ana, [{"role": "user", "content": "go"}], "sys", [], registry=reg
    )
    assert out == FALLBACK_TEXT
    assert len(client.requests) == 8   # MAX_ITERATIONS


async def test_request_carries_the_configured_model_and_caching(ana):
    client = FakeAnthropic([FakeResponse([TextBlock("ok")])])
    await run_agent(client, None, ana, [{"role": "user", "content": "hi"}], "sys", [])
    req = client.requests[0]
    assert req["model"] == "claude-opus-5"
    assert req["max_tokens"] == 8000
    assert req["output_config"] == {"effort": "low"}
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
```

- [ ] **Step 3: Run it and watch it fail**

Run: `pytest tests/test_llm.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia.core.llm'`

- [ ] **Step 4: Write `gaia/core/llm.py`**

```python
import logging

from gaia.capabilities.base import registry as default_registry
from gaia.core.config import settings
from gaia.core.models import User

log = logging.getLogger("gaia.llm")

MAX_ITERATIONS = 8
MAX_TOKENS = 8000
FALLBACK_TEXT = "Sorry — I couldn't work that one out. Could you try rephrasing?"


async def run_agent(
    client,
    conn,
    user: User,
    messages: list[dict],
    system: str,
    tool_defs: list[dict],
    registry=None,
) -> str:
    """Tool-use loop until the model produces text, or the cap is reached.

    Never returns an empty string: WhatsApp rejects an empty body, and a
    refusal or a max_tokens stop can legitimately produce no text block.
    """
    reg = registry or default_registry
    # The system prompt is stable per user, so it is the cache breakpoint.
    system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    for _ in range(MAX_ITERATIONS):
        response = await client.messages.create(
            model=settings.model,
            max_tokens=MAX_TOKENS,
            output_config={"effort": "low"},
            system=system_blocks,
            tools=tool_defs,
            messages=messages,
        )

        if response.stop_reason == "refusal":
            log.warning("model refused for user %s", user.id)
            return FALLBACK_TEXT

        if response.stop_reason != "tool_use":
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            if response.stop_reason == "max_tokens":
                log.warning("hit max_tokens for user %s", user.id)
            return text or FALLBACK_TEXT

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                output = await reg.dispatch(conn, user, block.name, block.input)
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": output}
                )
        messages.append({"role": "user", "content": results})

    log.warning("iteration cap reached for user %s", user.id)
    return FALLBACK_TEXT
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_llm.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add gaia/core/llm.py tests/test_llm.py tests/fakes.py
git commit -m "feat: agent loop with explicit stop-reason handling

Never returns an empty string: refusal, max_tokens and the iteration cap
all produce a fallback, because WhatsApp rejects an empty body and the
prototype would have sent one.

max_tokens is 8000 rather than 1500 — thinking is on by default on current
models and its tokens count against the budget."
```

---

### Task 11: WhatsApp client

**Files:**
- Create: `gaia/core/whatsapp.py`, `gaia/core/images.py`
- Create: `tests/test_whatsapp.py`, `tests/test_images.py`

**Interfaces:**
- Produces:
  - `parse_messages(payload: dict) -> list[dict]` — keys `id`, `from`, `type`, and `text` or `image_id`/`caption`.
  - `verify_signature(body: bytes, header: str) -> bool`
  - `WhatsAppClient.send_text(to, body)`, `.send_template(to, body)`, `.download_media(media_id) -> dict`
  - `images.downscale(raw: bytes) -> tuple[str, str]` — returns `(media_type, base64_data)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_whatsapp.py`:

```python
import hashlib
import hmac

from gaia.core import whatsapp


def test_parse_extracts_a_text_message():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.1", "from": "13055550001", "type": "text", "text": {"body": "hello"}}
    ]}}]}]}
    assert whatsapp.parse_messages(payload) == [
        {"id": "wamid.1", "from": "13055550001", "type": "text", "text": "hello"}
    ]


def test_parse_extracts_an_image_with_a_caption():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.2", "from": "1305", "type": "image",
         "image": {"id": "media.9", "caption": "my notes"}}
    ]}}]}]}
    [msg] = whatsapp.parse_messages(payload)
    assert msg["image_id"] == "media.9"
    assert msg["caption"] == "my notes"


def test_parse_ignores_status_callbacks():
    assert whatsapp.parse_messages({"entry": [{"changes": [{"value": {"statuses": [{}]}}]}]}) == []


def test_unsupported_types_degrade_to_text():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.3", "from": "1305", "type": "sticker"}
    ]}}]}]}
    [msg] = whatsapp.parse_messages(payload)
    assert msg["type"] == "text"
    assert "sticker" in msg["text"]


def test_signature_verification(monkeypatch):
    monkeypatch.setattr(whatsapp.settings, "wa_app_secret", "shh")
    body = b'{"hello":"world"}'
    good = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    assert whatsapp.verify_signature(body, good) is True
    assert whatsapp.verify_signature(body, "sha256=deadbeef") is False
    assert whatsapp.verify_signature(body, "") is False
```

`tests/test_images.py`:

```python
import base64
import io

from PIL import Image

from gaia.core.images import MAX_EDGE, downscale


def _jpeg(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="JPEG")
    return buf.getvalue()


def test_large_images_are_downscaled():
    media_type, data = downscale(_jpeg(5000, 3000))
    assert media_type == "image/jpeg"
    out = Image.open(io.BytesIO(base64.b64decode(data)))
    assert max(out.size) == MAX_EDGE


def test_small_images_are_left_alone():
    _, data = downscale(_jpeg(800, 600))
    assert Image.open(io.BytesIO(base64.b64decode(data))).size == (800, 600)
```

- [ ] **Step 2: Run and watch them fail**

Run: `pytest tests/test_whatsapp.py tests/test_images.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write `gaia/core/images.py`**

```python
import base64
import io

from PIL import Image

MAX_EDGE = 2576  # the model's high-resolution ceiling; more pixels cost tokens
                 # and buy nothing


def downscale(raw: bytes) -> tuple[str, str]:
    """Return (media_type, base64) sized for the model."""
    image = Image.open(io.BytesIO(raw))
    if image.mode != "RGB":
        image = image.convert("RGB")
    if max(image.size) > MAX_EDGE:
        image.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=88)
    return "image/jpeg", base64.b64encode(buf.getvalue()).decode()
```

- [ ] **Step 4: Write `gaia/core/whatsapp.py`**

```python
import hashlib
import hmac
import logging

import httpx

from gaia.core.config import settings
from gaia.core.images import downscale

log = logging.getLogger("gaia.whatsapp")

GRAPH = "https://graph.facebook.com/v21.0"
MAX_BODY = 4000  # WhatsApp caps at 4096
DIGEST_TEMPLATE = "daily_digest"


def verify_signature(body: bytes, header: str) -> bool:
    if not header:
        return False
    expected = "sha256=" + hmac.new(
        settings.wa_app_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(header, expected)


def parse_messages(payload: dict) -> list[dict]:
    out: list[dict] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages", []):
                base = {"id": m["id"], "from": m["from"], "type": m["type"]}
                if m["type"] == "text":
                    base["text"] = m["text"]["body"]
                elif m["type"] == "image":
                    base["image_id"] = m["image"]["id"]
                    base["caption"] = m["image"].get("caption")
                else:
                    base["type"] = "text"
                    base["text"] = f"[unsupported message type: {m['type']}]"
                out.append(base)
    return out


class WhatsAppClient:
    def __init__(self, token: str | None = None, phone_number_id: str | None = None):
        self._headers = {"Authorization": f"Bearer {token or settings.wa_access_token}"}
        self._phone_number_id = phone_number_id or settings.wa_phone_number_id

    async def _post(self, payload: dict) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{GRAPH}/{self._phone_number_id}/messages",
                headers=self._headers,
                json=payload,
            )
        if response.status_code >= 400:
            # The prototype ignored this, so 24h-window rejections were silent.
            log.error("whatsapp send failed %s: %s", response.status_code, response.text)

    async def send_text(self, to: str, body: str) -> None:
        for i in range(0, len(body) or 1, MAX_BODY):
            await self._post({
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body[i:i + MAX_BODY] or " "},
            })

    async def send_template(self, to: str, body: str) -> None:
        """Used outside the 24-hour customer service window, where free-form
        messages are rejected."""
        await self._post({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": DIGEST_TEMPLATE,
                "language": {"code": "en"},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": body[:1000]}]}
                ],
            },
        })

    async def download_media(self, media_id: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            meta = (await client.get(f"{GRAPH}/{media_id}", headers=self._headers)).json()
            if "url" not in meta:
                raise RuntimeError(f"no media url for {media_id}: {meta}")
            blob = await client.get(meta["url"], headers=self._headers)
            blob.raise_for_status()

        media_type, data = downscale(blob.content)
        return {"media_type": media_type, "data": data}
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_whatsapp.py tests/test_images.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
git add gaia/core/whatsapp.py gaia/core/images.py tests
git commit -m "feat: WhatsApp client with checked sends and image downscaling

Every send checks the response status. The prototype ignored it, so
24-hour-window rejections — the exact failure its own README predicted —
produced no log line at all.

send_template covers sends outside that window. Images are downscaled to
2576px, the model's resolution ceiling; beyond it pixels cost tokens and
buy nothing."
```

---

### Task 12: Walking skeleton — webhook, turns, and first deploy

**This is the deploy checkpoint.** After this task the full path — Meta → Caddy → FastAPI → Postgres → reply — is live with no capabilities registered. Prove the integration with 400 lines rather than 4,000.

**Files:**
- Create: `gaia/core/turns.py`, `gaia/butler.py`, `gaia/main.py`
- Create: `Dockerfile`, `Caddyfile`, `.env.example`
- Modify: `docker-compose.yml`
- Create: `tests/test_turns.py`, `tests/test_webhook.py`

**Interfaces:**
- Produces:
  - `turns.TurnQueue.submit(user, message) -> None` — debounces then runs the handler once per burst.
  - `butler.handle_turn(conn, user, messages: list[dict], wa) -> None`
  - `main.app` — FastAPI app with `GET /webhook`, `POST /webhook`, `GET /health`.

- [ ] **Step 1: Write the failing turn test**

`tests/test_turns.py`:

```python
import asyncio

from gaia.core.turns import TurnQueue


async def test_a_burst_becomes_one_turn(ana):
    seen = []

    async def handler(user, batch):
        seen.append([m["text"] for m in batch])

    q = TurnQueue(handler, debounce=0.05)
    await q.submit(ana, {"text": "notes"})
    await q.submit(ana, {"text": "photo"})
    await q.submit(ana, {"text": "book Tuesday"})
    await q.drain()

    assert seen == [["notes", "photo", "book Tuesday"]]


async def test_messages_arriving_mid_turn_queue_rather_than_racing(ana):
    running = []
    concurrent = []

    async def handler(user, batch):
        concurrent.append(len(running))
        running.append(1)
        await asyncio.sleep(0.05)
        running.pop()

    q = TurnQueue(handler, debounce=0.01)
    await q.submit(ana, {"text": "first"})
    await asyncio.sleep(0.02)          # let the first turn start
    await q.submit(ana, {"text": "second"})
    await q.drain()

    assert concurrent == [0, 0], "two turns ran concurrently for one user"


async def test_different_users_are_not_serialised(ana, sofia):
    order = []

    async def handler(user, batch):
        order.append(user.name)
        await asyncio.sleep(0.05)

    q = TurnQueue(handler, debounce=0.01)
    await q.submit(ana, {"text": "a"})
    await q.submit(sofia, {"text": "b"})
    await q.drain()

    assert sorted(order) == ["Ana", "Sofia"]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_turns.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia.core.turns'`

- [ ] **Step 3: Write `gaia/core/turns.py`**

```python
import asyncio
import logging
from collections import defaultdict

from gaia.core.models import User

log = logging.getLogger("gaia.turns")


class TurnQueue:
    """One turn at a time, per user, with a debounce window.

    People send three messages in five seconds — notes, a photo, then "oh and
    book Tuesday". Without this each becomes its own agent loop: interleaved
    tool calls, three overlapping replies, racing writes to the same contact.
    """

    def __init__(self, handler, debounce: float = 3.0):
        self._handler = handler
        self._debounce = debounce
        self._pending: dict[str, list[dict]] = defaultdict(list)
        self._timers: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._running: set[asyncio.Task] = set()

    async def submit(self, user: User, message: dict) -> None:
        key = user.wa_id
        self._pending[key].append(message)

        if timer := self._timers.get(key):
            timer.cancel()
        self._timers[key] = asyncio.create_task(self._fire_after_debounce(user))

    async def _fire_after_debounce(self, user: User) -> None:
        key = user.wa_id
        try:
            await asyncio.sleep(self._debounce)
        except asyncio.CancelledError:
            return  # a newer message joined the burst

        batch = self._pending.pop(key, [])
        self._timers.pop(key, None)
        if not batch:
            return

        task = asyncio.create_task(self._run(user, batch))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _run(self, user: User, batch: list[dict]) -> None:
        async with self._locks[user.wa_id]:
            try:
                await self._handler(user, batch)
            except Exception:
                log.exception("turn failed for user %s", user.id)

    async def drain(self) -> None:
        """Test helper: wait for every debounce timer and turn to finish."""
        while self._timers or self._running:
            await asyncio.gather(*list(self._timers.values()), return_exceptions=True)
            await asyncio.gather(*list(self._running), return_exceptions=True)
```

- [ ] **Step 4: Write `gaia/butler.py`**

```python
import logging

from gaia.capabilities.base import registry
from gaia.core.db import contacts as contacts_db
from gaia.core.db import messages as messages_db
from gaia.core.db import users as users_db
from gaia.core.db.pool import tx
from gaia.core.llm import run_agent
from gaia.core.models import User

log = logging.getLogger("gaia.butler")

BASE_PROMPT = """You are the assistant for {name}, an agent at the Gaia real-estate company, \
reachable over WhatsApp.

When she sends meeting notes — typed or photographed handwriting — transcribe if needed, then \
extract a short summary, the people involved, commitments made, and any follow-up dates. Save \
them with your tools. Echo back what you understood and ask her to confirm anything ambiguous: \
names, numbers, dates.

Answer questions about past meetings, leads and contacts using your tools. Never contact third \
parties.

Style: brief and warm, like a text message. No markdown headers or bullet lists.

Today is {today} in her timezone ({tz}).
People she has worked with recently: {roster}
Use lookup_contact for details on any of them.
"""


async def build_system_prompt(conn, user: User) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    names = await contacts_db.roster(conn, user)
    today = datetime.now(ZoneInfo(user.timezone)).date().isoformat()
    return BASE_PROMPT.format(
        name=user.name,
        today=today,
        tz=user.timezone,
        roster=", ".join(names) or "(nobody yet)",
    ) + registry.prompt_fragments(user)


async def handle_turn(user: User, batch: list[dict], wa) -> None:
    """One agent turn for one user, over a whole debounced burst."""
    async with tx() as conn:
        await users_db.touch_inbound(conn, user)

        blocks, wa_ids = [], []
        for message in batch:
            wa_ids.append(message["id"])
            if message["type"] == "image":
                media = await wa.download_media(message["image_id"])
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", **media},
                })
                caption = message.get("caption") or "Here are my meeting notes."
                blocks.append({"type": "text", "text": caption})
                await messages_db.log(conn, user, "user", f"[photo] {caption}", message["id"])
            else:
                blocks.append({"type": "text", "text": message["text"]})
                await messages_db.log(conn, user, "user", message["text"], message["id"])

        history = await messages_db.recent(conn, user, exclude_wa_ids=tuple(wa_ids))
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": blocks})

        system = await build_system_prompt(conn, user)
        tool_defs = registry.tool_defs(user)

    from anthropic import AsyncAnthropic

    from gaia.core.config import settings

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    async with tx() as conn:
        reply = await run_agent(client, conn, user, messages, system, tool_defs)
        await messages_db.log(conn, user, "assistant", reply)

    await wa.send_text(user.wa_id, reply)
```

- [ ] **Step 5: Write `gaia/main.py`**

```python
import logging

from fastapi import FastAPI, HTTPException, Request, Response

from gaia.core import whatsapp
from gaia.core.config import settings
from gaia.core.db import messages as messages_db
from gaia.core.db import users as users_db
from gaia.core.db.migrate import run_migrations
from gaia.core.db.pool import get_pool, tx
from gaia.core.turns import TurnQueue
from gaia.butler import handle_turn

import gaia.capabilities  # noqa: F401  — importing populates the registry

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gaia")

app = FastAPI()
wa = whatsapp.WhatsAppClient()


async def _handler(user, batch):
    await handle_turn(user, batch, wa)


queue = TurnQueue(_handler, debounce=settings.debounce_seconds)


@app.on_event("startup")
async def startup() -> None:
    pool = get_pool()
    await pool.open(wait=True)
    applied = await run_migrations(pool)
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))


@app.get("/health")
async def health() -> dict:
    async with tx() as conn:
        await conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/webhook")
async def verify(request: Request) -> Response:
    params = request.query_params
    if params.get("hub.verify_token") == settings.wa_verify_token:
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(status_code=403)


@app.post("/webhook")
async def inbound(request: Request) -> dict:
    body = await request.body()
    if not whatsapp.verify_signature(body, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(status_code=403)

    payload = await request.json()
    for message in whatsapp.parse_messages(payload):
        async with tx() as conn:
            user = await users_db.get_by_wa_id(conn, message["from"])
            if user is None:
                log.info("ignoring message from unknown number %s", message["from"])
                continue
            if await messages_db.seen(conn, message["id"]):
                continue
        await queue.submit(user, message)

    # Returns before the agent loop runs. Meta retries slow webhooks and
    # eventually disables the subscription over them.
    return {"status": "ok"}
```

- [ ] **Step 6: Write the webhook test**

`tests/test_webhook.py`:

```python
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, migrated):
    from gaia.core import whatsapp
    from gaia.core.db import pool as pool_module

    monkeypatch.setattr(whatsapp.settings, "wa_app_secret", "shh")
    monkeypatch.setattr(pool_module, "_pool", migrated)

    from gaia.main import app

    with TestClient(app) as c:
        yield c


def _signed(client, payload: dict, secret: bytes = b"shh"):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=body, headers={"x-hub-signature-256": sig})


def test_bad_signature_is_rejected(client):
    body = json.dumps({"entry": []}).encode()
    r = client.post("/webhook", content=body, headers={"x-hub-signature-256": "sha256=bad"})
    assert r.status_code == 403


def test_missing_signature_is_rejected(client):
    assert client.post("/webhook", content=b"{}").status_code == 403


def test_verification_challenge_is_echoed(client, monkeypatch):
    from gaia.core.config import settings

    monkeypatch.setattr(settings, "wa_verify_token", "letmein")
    r = client.get("/webhook", params={"hub.verify_token": "letmein", "hub.challenge": "42"})
    assert r.status_code == 200
    assert r.text == "42"


def test_wrong_verify_token_is_rejected(client):
    r = client.get("/webhook", params={"hub.verify_token": "nope", "hub.challenge": "42"})
    assert r.status_code == 403


def test_unknown_sender_is_accepted_but_ignored(client):
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.x", "from": "19998887777", "type": "text", "text": {"body": "hi"}}
    ]}}]}]}
    assert _signed(client, payload).status_code == 200


def test_health_reports_ok(client):
    assert client.get("/health").json() == {"status": "ok"}
```

- [ ] **Step 7: Write the Dockerfile**

`Dockerfile`:

```dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 TZ=America/New_York
WORKDIR /srv

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY gaia ./gaia
COPY migrations ./migrations

# Exactly one worker. The per-user turn lock is in-process, so a second
# worker would silently reintroduce concurrent turns for the same user.
CMD ["uvicorn", "gaia.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

`Caddyfile`:

```
{$DOMAIN} {
    reverse_proxy app:8000
}
```

`.env.example`:

```
DB_PASSWORD=change-me
DOMAIN=agent.gaia.example.com
DATABASE_URL=postgresql://gaia:change-me@db:5432/gaia
ANTHROPIC_API_KEY=sk-ant-...
VOYAGE_API_KEY=pa-...
WA_ACCESS_TOKEN=EAAG...
WA_APP_SECRET=...
WA_VERIFY_TOKEN=any-string-you-invent
WA_PHONE_NUMBER_ID=1234567890
MODEL=claude-opus-5
```

- [ ] **Step 8: Add app and caddy to `docker-compose.yml`**

```yaml
  app:
    build: .
    restart: unless-stopped
    env_file: .env
    environment:
      TZ: America/New_York
    depends_on:
      db:
        condition: service_healthy

  caddy:
    image: caddy:2
    restart: unless-stopped
    env_file: .env          # without this {$DOMAIN} expands to nothing
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
    depends_on:
      - app
```

Add `caddy_data:` to the `volumes:` block.

- [ ] **Step 9: Run the full suite**

Run: `pytest -v`
Expected: PASS

- [ ] **Step 10: Deploy the skeleton and verify end to end**

```bash
# On the droplet, after DNS points at it and .env is filled in:
docker compose up -d --build
docker compose logs -f app
```

Then in Meta's console point the webhook at `https://<DOMAIN>/webhook`, subscribe
to `messages`, and add yourself with
`docker compose exec app python -m gaia.core.admin add-user --name "<you>" --phone <number> --role admin`.

Text the number. Expect a reply from the fallback path — no capabilities are
registered yet, so the model has no tools. **What this proves:** DNS, Caddy's
certificate, Meta's signature, the pool, migrations on startup, user resolution,
the turn queue, and the send path.

- [ ] **Step 11: Commit**

```bash
git add gaia/core/turns.py gaia/butler.py gaia/main.py Dockerfile Caddyfile .env.example docker-compose.yml tests
git commit -m "feat: webhook, turn queue, and a deployable skeleton

The webhook verifies the signature, resolves the sender, dedups, and
returns 200 before the agent loop runs — the prototype ran the full loop
inline, which Meta retries and eventually disables the subscription over.

TurnQueue debounces a burst into one turn and serialises per user. It
requires a single uvicorn worker, enforced in the Dockerfile CMD."
```

---

### Task 13: Meetings capability

**Files:**
- Create: `gaia/capabilities/meetings/__init__.py`, `gaia/capabilities/meetings/tools.py`
- Modify: `gaia/capabilities/__init__.py`
- Create: `tests/test_capability_meetings.py`

**Interfaces:**
- Consumes: `meetings.save`, `memory.index_meeting`, `memory.search`, `contacts.lookup`, `contacts.merge_profile`.
- Produces: capability `meetings` with tools `save_meeting`, `search_memory`, `lookup_contact`.

- [ ] **Step 1: Write the failing test**

`tests/test_capability_meetings.py`:

```python
from gaia.capabilities.meetings import CAPABILITY, save_meeting, search_memory


async def test_save_meeting_files_everything(conn, ana):
    result = await save_meeting(conn, ana, {
        "summary": "Showed Coral Gables to the Delgados",
        "contacts": [{"name": "Maria Delgado", "profile_update": "wants a pool"}],
        "commitments": [{"description": "Send comps", "contact_name": "Maria Delgado"}],
    })
    assert result["saved"] is True

    hits = await search_memory(conn, ana, {"query": "Coral Gables"})
    assert any("Coral Gables" in h["content"] for h in hits["results"])


async def test_save_meeting_respects_private(conn, ana, sofia):
    await save_meeting(conn, ana, {
        "summary": "Quiet divorce sale",
        "contacts": [{"name": "Rivera"}],
        "private": True,
    })
    assert (await search_memory(conn, sofia, {"query": "divorce"}))["results"] == []
    assert (await search_memory(conn, ana, {"query": "divorce"}))["results"] != []


def test_capability_is_public():
    assert CAPABILITY.allowed_roles is None
    assert CAPABILITY.allowed_user_ids is None
    assert {t.name for t in CAPABILITY.tools} == {
        "save_meeting", "search_memory", "lookup_contact"
    }
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_capability_meetings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia.capabilities.meetings'`

- [ ] **Step 3: Write `gaia/capabilities/meetings/tools.py`**

```python
from gaia.core.db import contacts as contacts_db
from gaia.core.db import meetings as meetings_db
from gaia.core.db import memory as memory_db
from gaia.core.models import User


async def save_meeting(conn, user: User, args: dict) -> dict:
    visibility = "private" if args.get("private") else "org"
    contacts = args.get("contacts", [])

    meeting_id = await meetings_db.save(
        conn,
        user,
        summary=args["summary"],
        source="photo_notes" if args.get("raw_transcription") else "text",
        raw_input=args.get("raw_transcription"),
        happened_at=args.get("happened_at"),
        visibility=visibility,
        contact_names=tuple(c["name"] for c in contacts),
        commitments=tuple(args.get("commitments", [])),
    )

    first_contact_id = None
    for entry in contacts:
        contact_id = await contacts_db.get_or_create(conn, user, entry["name"])
        first_contact_id = first_contact_id or contact_id
        if entry.get("profile_update"):
            await contacts_db.merge_profile(conn, user, contact_id, entry["profile_update"])

    # Visibility comes from the meeting, never defaulted — a private note whose
    # embedding is org-visible is searchable by the whole company.
    await memory_db.index_meeting(
        conn, user, meeting_id, args["summary"], visibility, first_contact_id
    )

    return {
        "saved": True,
        "meeting_id": str(meeting_id),
        "contacts": [c["name"] for c in contacts],
        "visibility": visibility,
    }


async def search_memory(conn, user: User, args: dict) -> dict:
    results = await memory_db.search(
        conn, user, args["query"], contact_name=args.get("contact_name")
    )
    return {"results": results}


async def lookup_contact(conn, user: User, args: dict) -> dict:
    found = await contacts_db.lookup(conn, user, args["name"])
    return {"contact": found} if found else {"contact": None, "note": "no such contact"}
```

- [ ] **Step 4: Write `gaia/capabilities/meetings/__init__.py`**

```python
from gaia.capabilities.base import Capability, Tool
from gaia.capabilities.meetings.tools import lookup_contact, save_meeting, search_memory

SAVE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "raw_transcription": {
            "type": "string",
            "description": "Full transcription when the source was a photo",
        },
        "happened_at": {
            "type": "string",
            "description": "ISO 8601. Use when the meeting was not today — notes "
                           "photographed the next morning belong to the day they happened.",
        },
        "private": {
            "type": "boolean",
            "description": "True if she asks to keep this off the company record. "
                           "Defaults to false: colleagues can see it.",
        },
        "contacts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "profile_update": {
                        "type": "string",
                        "description": "New facts about this person to merge into their profile",
                    },
                },
                "required": ["name"],
            },
        },
        "commitments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "due_at": {"type": "string", "description": "ISO 8601, optional"},
                    "contact_name": {"type": "string"},
                },
                "required": ["description"],
            },
        },
    },
    "required": ["summary", "contacts"],
}

CAPABILITY = Capability(
    name="meetings",
    description="Filing and recalling meeting notes",
    prompt_fragment=(
        "\nWhen she asks you to keep something off the company record, pass private: true "
        "to save_meeting. Otherwise colleagues at Gaia can see it, which is the default.\n"
    ),
    tools=(
        Tool("save_meeting",
             "Save a meeting after extracting structure: creates the meeting, links contacts "
             "(creating them if new), stores commitments, and indexes the summary into "
             "semantic memory.",
             SAVE_SCHEMA, save_meeting),
        Tool("search_memory",
             "Semantic search over past meetings and notes. Use for questions about history.",
             {"type": "object",
              "properties": {"query": {"type": "string"},
                             "contact_name": {"type": "string", "description": "Optional filter"}},
              "required": ["query"]},
             search_memory),
        Tool("lookup_contact",
             "Fetch what is known about one person: profile, phone, email.",
             {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
             lookup_contact),
    ),
)
```

- [ ] **Step 5: Register it in `gaia/capabilities/__init__.py`**

```python
"""Capability registration. Importing this module populates the registry.

Adding a capability is: a directory here, a Capability instance, one import.
"""

from gaia.capabilities.base import registry
from gaia.capabilities.meetings import CAPABILITY as MEETINGS

registry.register(MEETINGS)

__all__ = ["registry"]
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_capability_meetings.py -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Commit**

```bash
git add gaia/capabilities tests/test_capability_meetings.py
git commit -m "feat: meetings capability

save_meeting, search_memory and lookup_contact. Visibility flows from the
meeting into its memory chunk rather than defaulting, and lookup_contact
replaces injecting every profile into every request."
```

---

### Task 14: Leads capability

**Files:**
- Create: `gaia/capabilities/leads/__init__.py`, `gaia/capabilities/leads/tools.py`
- Modify: `gaia/capabilities/__init__.py`
- Create: `tests/test_capability_leads.py`

**Interfaces:**
- Produces: capability `leads` with tools `create_lead`, `query_leads`, `update_lead`, `complete_commitment`.

- [ ] **Step 1: Write the failing test**

`tests/test_capability_leads.py`:

```python
from gaia.capabilities.leads import CAPABILITY, create_lead, query_leads, update_lead


async def test_create_then_query(conn, ana):
    created = await create_lead(conn, ana, {
        "contact_name": "Maria Delgado",
        "description": "Buying in Coral Gables, ~600k",
        "next_action_at": "2026-09-15T14:00:00Z",
        "next_action_note": "Send Friday listings",
    })
    assert created["created"] is True

    rows = (await query_leads(conn, ana, {}))["leads"]
    assert len(rows) == 1
    assert rows[0]["name"] == "Maria Delgado"


async def test_update_marks_a_lead_lost(conn, ana):
    created = await create_lead(conn, ana, {"contact_name": "Rivera", "description": "Selling"})
    result = await update_lead(conn, ana, {"lead_id": created["lead_id"], "status": "lost"})
    assert result["updated"] is True
    assert (await query_leads(conn, ana, {}))["leads"][0]["status"] == "lost"


async def test_a_users_private_lead_is_invisible_to_others(conn, ana, sofia):
    await create_lead(conn, ana, {
        "contact_name": "Quiet Client", "description": "Discreet sale", "private": True,
    })
    assert (await query_leads(conn, sofia, {}))["leads"] == []


def test_capability_is_public():
    assert CAPABILITY.allowed_roles is None
    assert {t.name for t in CAPABILITY.tools} == {
        "create_lead", "query_leads", "update_lead", "complete_commitment"
    }
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_capability_leads.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia.capabilities.leads'`

- [ ] **Step 3: Write `gaia/capabilities/leads/tools.py`**

```python
from gaia.core.db import commitments as commitments_db
from gaia.core.db import leads as leads_db
from gaia.core.models import User


async def create_lead(conn, user: User, args: dict) -> dict:
    lead_id = await leads_db.create(
        conn,
        user,
        contact_name=args["contact_name"],
        description=args["description"],
        status=args.get("status", "new"),
        next_action_at=args.get("next_action_at"),
        next_action_note=args.get("next_action_note"),
        visibility="private" if args.get("private") else "org",
    )
    return {"created": True, "lead_id": str(lead_id)}


async def query_leads(conn, user: User, args: dict) -> dict:
    rows = await leads_db.query(
        conn, user, due_only=args.get("due_only", False), status=args.get("status")
    )
    return {"leads": rows}


async def update_lead(conn, user: User, args: dict) -> dict:
    updated = await leads_db.update(
        conn,
        user,
        args["lead_id"],
        status=args.get("status"),
        next_action_at=args.get("next_action_at"),
        next_action_note=args.get("next_action_note"),
        description=args.get("description"),
    )
    return {"updated": updated} if updated else {
        "updated": False, "note": "no such lead, or it belongs to someone else"
    }


async def complete_commitment(conn, user: User, args: dict) -> dict:
    done = await commitments_db.complete(conn, user, args["commitment_id"])
    return {"completed": done}
```

- [ ] **Step 4: Write `gaia/capabilities/leads/__init__.py`**

```python
from gaia.capabilities.base import Capability, Tool
from gaia.capabilities.leads.tools import (
    complete_commitment,
    create_lead,
    query_leads,
    update_lead,
)

STATUSES = ["new", "active", "under_contract", "closed", "lost", "dormant"]

CAPABILITY = Capability(
    name="leads",
    description="Tracking deals and follow-ups",
    prompt_fragment=(
        "\nWhen she mentions someone who might transact, create a lead with a next action "
        "date so it reaches her morning digest. A lead without a next_action_at will never "
        "be followed up.\n"
    ),
    tools=(
        Tool("create_lead",
             "Create a lead for a contact, with the next follow-up date. Creates the contact "
             "if they are new.",
             {"type": "object",
              "properties": {
                  "contact_name": {"type": "string"},
                  "description": {"type": "string",
                                  "description": "e.g. 'buying in Coral Gables, ~600k'"},
                  "status": {"type": "string", "enum": STATUSES},
                  "next_action_at": {"type": "string", "description": "ISO 8601"},
                  "next_action_note": {"type": "string",
                                       "description": "e.g. 'send Friday listing update'"},
                  "private": {"type": "boolean",
                              "description": "Keep off the company record. Defaults to false."},
              },
              "required": ["contact_name", "description"]},
             create_lead),
        Tool("query_leads",
             "List leads, optionally filtered by status or by whether follow-up is due.",
             {"type": "object",
              "properties": {"due_only": {"type": "boolean"},
                             "status": {"type": "string", "enum": STATUSES}}},
             query_leads),
        Tool("update_lead",
             "Update a lead's status, next action date, or note.",
             {"type": "object",
              "properties": {"lead_id": {"type": "string"},
                             "status": {"type": "string", "enum": STATUSES},
                             "next_action_at": {"type": "string"},
                             "next_action_note": {"type": "string"},
                             "description": {"type": "string"}},
              "required": ["lead_id"]},
             update_lead),
        Tool("complete_commitment",
             "Mark a commitment done.",
             {"type": "object", "properties": {"commitment_id": {"type": "string"}},
              "required": ["commitment_id"]},
             complete_commitment),
    ),
)
```

- [ ] **Step 5: Register it**

In `gaia/capabilities/__init__.py`, add:

```python
from gaia.capabilities.leads import CAPABILITY as LEADS

registry.register(LEADS)
```

- [ ] **Step 6: Run the full suite**

Run: `pytest -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add gaia/capabilities tests/test_capability_leads.py
git commit -m "feat: leads capability with create_lead

The tool the prototype never had. Its prompt fragment says a lead without
a next_action_at will never be followed up, because that is the failure
mode the schema allows and the model should avoid."
```

---

### Task 15: Morning digest

**Files:**
- Create: `gaia/jobs/__init__.py`, `gaia/jobs/digest.py`
- Create: `tests/test_digest.py`
- Modify: `docker-compose.yml` (add the `jobs` service)

**Interfaces:**
- Produces:
  - `due_users(conn, now_utc) -> list[User]` — active users whose local time is past 08:00 and whose `last_digest_on` is not today.
  - `compose_digest(client, user, leads, commitments) -> str`
  - `send_digest(conn, client, wa, user) -> bool` — returns whether anything was sent.
  - `run_once(pool, client, wa) -> int`

- [ ] **Step 1: Write the failing test**

`tests/test_digest.py`:

```python
from datetime import datetime, timedelta, timezone

from gaia.core.db import leads as leads_db
from gaia.jobs import digest
from tests.fakes import FakeAnthropic, FakeResponse, FakeWhatsApp, TextBlock


async def test_quiet_day_sends_nothing(conn, ana):
    wa = FakeWhatsApp()
    sent = await digest.send_digest(conn, FakeAnthropic([]), wa, ana)
    assert sent is False
    assert wa.sent == []


async def test_due_lead_produces_a_message(conn, ana):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await leads_db.create(
        conn, ana, contact_name="Maria Delgado", description="Buying",
        next_action_at=past, next_action_note="Send Friday listings",
    )
    wa = FakeWhatsApp()
    client = FakeAnthropic([FakeResponse([TextBlock("Morning! Maria Delgado is due.")])])

    assert await digest.send_digest(conn, client, wa, ana) is True
    assert wa.sent[0][0] == ana.wa_id
    assert "Maria" in wa.sent[0][1]


async def test_digest_excludes_a_colleagues_due_lead(conn, ana, sofia):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await leads_db.create(
        conn, sofia, contact_name="Rivera", description="Selling", next_action_at=past
    )
    wa = FakeWhatsApp()
    assert await digest.send_digest(conn, FakeAnthropic([]), wa, ana) is False


async def test_nudge_count_increments_and_reaches_the_prompt(conn, ana):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await leads_db.create(
        conn, ana, contact_name="Maria", description="Buying", next_action_at=past
    )
    wa = FakeWhatsApp()

    client = FakeAnthropic([FakeResponse([TextBlock("first")])])
    await digest.send_digest(conn, client, wa, ana)

    client2 = FakeAnthropic([FakeResponse([TextBlock("second")])])
    await digest.send_digest(conn, client2, wa, ana)

    prompt = str(client2.requests[0]["messages"])
    assert "nudge_count" in prompt and "1" in prompt


async def test_outside_the_window_a_template_is_used(conn, ana):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await leads_db.create(
        conn, ana, contact_name="Maria", description="Buying", next_action_at=past
    )
    await conn.execute(
        "UPDATE users SET last_inbound_at = now() - interval '30 hours' WHERE id = %s",
        (ana.id,),
    )
    wa = FakeWhatsApp()
    client = FakeAnthropic([FakeResponse([TextBlock("Morning!")])])

    await digest.send_digest(conn, client, wa, ana)
    assert wa.sent == [] and len(wa.templates) == 1
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_digest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia.jobs'`

- [ ] **Step 3: Write `gaia/jobs/digest.py`**

```python
"""Per-user morning digest.

Runs every 15 minutes and sends to each user whose *local* time has just
crossed 08:00, which is not expressible as a single cron line once users have
their own timezones.
"""

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from gaia.core.config import settings
from gaia.core.db import commitments as commitments_db
from gaia.core.db import leads as leads_db
from gaia.core.db import messages as messages_db
from gaia.core.db import users as users_db
from gaia.core.db.pool import tx
from gaia.core.models import User

log = logging.getLogger("gaia.digest")

SEND_HOUR = 8
WINDOW_HOURS = 24

SYSTEM = """Write a short, warm morning WhatsApp message for a real-estate agent listing what \
needs follow-up today. Group by person. Plain text, no markdown, no bullet characters.

Each item carries a nudge_count: how many mornings it has already appeared without being acted \
on. Vary the wording accordingly — 0 is new, 1-2 should note it is still open, and 3 or more \
should gently ask whether to snooze or close it. Never repeat yesterday's phrasing verbatim.

End by offering to draft any of the follow-up texts."""


async def due_users(conn, now_utc: datetime | None = None) -> list[User]:
    now_utc = now_utc or datetime.now(timezone.utc)
    out = []
    for user in await users_db.list_users(conn):
        if not user.active:
            continue
        local = now_utc.astimezone(ZoneInfo(user.timezone))
        if local.hour < SEND_HOUR:
            continue
        cur = await conn.execute(
            "SELECT last_digest_on FROM users WHERE id = %s", (user.id,)
        )
        last = (await cur.fetchone())["last_digest_on"]
        if last == local.date():
            continue  # already sent today; a restart must not double-send
        out.append(user)
    return out


async def _within_window(conn, user: User) -> bool:
    cur = await conn.execute(
        f"""SELECT last_inbound_at IS NOT NULL
                   AND last_inbound_at > now() - interval '{WINDOW_HOURS} hours' AS ok
            FROM users WHERE id = %s""",
        (user.id,),
    )
    return bool((await cur.fetchone())["ok"])


async def compose_digest(client, user: User, leads: list[dict], commitments: list[dict]) -> str:
    payload = {
        "leads": [
            {"contact": r["name"], "description": r["description"],
             "note": r["next_action_note"], "nudge_count": r["nudge_count"]}
            for r in leads
        ],
        "commitments": [
            {"description": r["description"], "contact": r["contact"],
             "due": r["due_at"].isoformat() if r["due_at"] else None,
             "nudge_count": r["nudge_count"]}
            for r in commitments
        ],
    }
    response = await client.messages.create(
        model=settings.model,
        max_tokens=2000,
        output_config={"effort": "low"},
        system=SYSTEM,
        messages=[{"role": "user", "content": str(payload)}],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()


async def send_digest(conn, client, wa, user: User) -> bool:
    """Returns whether anything was sent. A digest on an empty day trains
    people to ignore the thread."""
    leads = await leads_db.due_for(conn, user)
    commitments = await commitments_db.open_for(conn, user)
    if not leads and not commitments:
        return False

    text = await compose_digest(client, user, leads, commitments)
    if not text:
        log.warning("empty digest for user %s", user.id)
        return False

    if await _within_window(conn, user):
        await wa.send_text(user.wa_id, text)
    else:
        # Free-form sends are rejected outside the 24-hour service window.
        await wa.send_template(user.wa_id, text)

    await leads_db.mark_nudged(conn, user, [r["id"] for r in leads])
    await commitments_db.mark_nudged(conn, user, [r["id"] for r in commitments])
    await messages_db.log(conn, user, "assistant", text)
    await conn.execute(
        "UPDATE users SET last_digest_on = %s WHERE id = %s",
        (datetime.now(ZoneInfo(user.timezone)).date(), user.id),
    )
    return True


async def run_once(pool, client, wa) -> int:
    sent = 0
    async with tx(pool) as conn:
        users = await due_users(conn)
    for user in users:
        async with tx(pool) as conn:
            if await send_digest(conn, client, wa, user):
                sent += 1
    return sent


async def main() -> None:
    from anthropic import AsyncAnthropic

    from gaia.core.db.pool import get_pool
    from gaia.core.whatsapp import WhatsAppClient

    logging.basicConfig(level=logging.INFO)
    pool = get_pool()
    await pool.open(wait=True)
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    wa = WhatsAppClient()

    while True:
        try:
            count = await run_once(pool, client, wa)
            if count:
                log.info("sent %d digests", count)
        except Exception:
            log.exception("digest run failed")
        await asyncio.sleep(900)  # 15 minutes


if __name__ == "__main__":
    asyncio.run(main())
```

Create an empty `gaia/jobs/__init__.py`.

- [ ] **Step 4: Add the jobs service to `docker-compose.yml`**

```yaml
  jobs:
    build: .
    restart: unless-stopped
    env_file: .env
    environment:
      TZ: America/New_York
    command: ["python", "-m", "gaia.jobs.digest"]
    depends_on:
      db:
        condition: service_healthy
```

> Its own service, not in-container cron. Cron runs jobs with a near-empty
> environment rather than the container's, so the prototype's digest would have
> died on its first `os.environ` lookup every morning, silently.

- [ ] **Step 5: Run the full suite**

Run: `pytest -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add gaia/jobs tests/test_digest.py docker-compose.yml
git commit -m "feat: per-user morning digest

Scoped by ownership, so nobody is nagged about a colleague's follow-ups.
Silent on an empty day. nudge_count reaches the prompt so day three reads
'still open' and day four offers to snooze, rather than repeating an
identical line until she mutes the thread.

Outside the 24-hour service window it sends a template, and last_digest_on
makes a restart inside the 15-minute window idempotent.

Runs as its own compose service rather than in-container cron, which would
have executed with an empty environment."
```

---

### Task 16: Production deployment

**Files:**
- Create: `deploy/backup.sh`, `deploy/restore.sh`, `deploy/README.md`
- Modify: `docker-compose.yml` (backup service)

- [ ] **Step 1: Write the backup script**

`deploy/backup.sh`:

```bash
#!/usr/bin/env bash
# Nightly logical backup to DigitalOcean Spaces.
# DO droplet backups run weekly; losing six days of meeting notes is not an
# acceptable worst case for a company's client book.
set -euo pipefail

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FILE="/tmp/gaia-${STAMP}.dump"

pg_dump -Fc -h db -U gaia gaia > "$FILE"
aws s3 cp "$FILE" "s3://${SPACES_BUCKET}/backups/gaia-${STAMP}.dump" \
    --endpoint-url "${SPACES_ENDPOINT}"
rm -f "$FILE"

# 30-day retention.
CUTOFF=$(date -u -d '30 days ago' +%Y%m%d)
aws s3 ls "s3://${SPACES_BUCKET}/backups/" --endpoint-url "${SPACES_ENDPOINT}" \
  | awk '{print $4}' \
  | while read -r key; do
      d=$(echo "$key" | sed -n 's/gaia-\([0-9]\{8\}\)T.*/\1/p')
      [ -n "$d" ] && [ "$d" -lt "$CUTOFF" ] && \
        aws s3 rm "s3://${SPACES_BUCKET}/backups/${key}" --endpoint-url "${SPACES_ENDPOINT}"
    done
echo "backed up ${FILE##*/}"
```

`deploy/restore.sh`:

```bash
#!/usr/bin/env bash
# Restore a dump into a scratch database and report row counts.
# Run this BEFORE going live. An untested backup is not a backup.
set -euo pipefail
DUMP=${1:?usage: restore.sh <dump-file>}

createdb -h db -U gaia gaia_restore_test 2>/dev/null || true
pg_restore -h db -U gaia -d gaia_restore_test --clean --if-exists "$DUMP"

psql -h db -U gaia -d gaia_restore_test -c "
  SELECT 'users' t, count(*) FROM users
  UNION ALL SELECT 'contacts', count(*) FROM contacts
  UNION ALL SELECT 'meetings', count(*) FROM meetings
  UNION ALL SELECT 'leads', count(*) FROM leads
  UNION ALL SELECT 'memory_chunks', count(*) FROM memory_chunks;"

echo "Restore OK. Drop with: dropdb -h db -U gaia gaia_restore_test"
```

- [ ] **Step 2: Add the backup service**

```yaml
  backup:
    image: postgres:17-alpine
    restart: unless-stopped
    env_file: .env
    environment:
      PGPASSWORD: ${DB_PASSWORD}
      TZ: America/New_York
    volumes:
      - ./deploy:/deploy:ro
    entrypoint: ["/bin/sh", "-c"]
    command: >
      "apk add --no-cache aws-cli >/dev/null &&
       while true; do sleep 86400; /deploy/backup.sh || echo 'backup failed'; done"
    depends_on:
      db:
        condition: service_healthy
```

- [ ] **Step 3: Write `deploy/README.md`**

````markdown
# Deploying gaia-butler

## Droplet

Ubuntu 24.04 LTS, 2GB / 1 vCPU / 50GB, NYC region.

```bash
# 2GB swap — Postgres, Python and Caddy on 2GB have no headroom otherwise
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

curl -fsSL https://get.docker.com | sh
```

**DO cloud firewall:** inbound 80 and 443 from anywhere, 22 from your IP only.
Postgres publishes no ports and is reachable only on the compose network.

## DNS

An A record for `$DOMAIN` pointing at the droplet. Caddy provisions TLS on first
boot; it needs port 80 reachable to do so.

## Meta

1. A phone number not already on personal WhatsApp.
2. A permanent access token via Business Settings → System Users.
3. Webhook `https://$DOMAIN/webhook`, verify token matching `WA_VERIFY_TOKEN`,
   subscribed to `messages`.
4. **A `daily_digest` utility template**, one body parameter. Needed for the
   morning digest to reach anyone who has not messaged in 24 hours. Submit this
   early — approval time is not under your control.

## First run

```bash
git clone <repo> && cd gaia-assistant
cp .env.example .env      # fill in
docker compose up -d --build

docker compose exec app python -m gaia.core.admin \
    add-user --name "<name>" --phone <number> --role admin
```

## Before going live: prove the restore

```bash
docker compose exec backup /deploy/backup.sh
docker compose exec backup /deploy/restore.sh /tmp/gaia-<stamp>.dump
```

Row counts must match production. This is the step people skip and regret.

## Adding an agent

```bash
docker compose exec app python -m gaia.core.admin add-user --name "Ana" --phone 13055550001
docker compose exec app python -m gaia.core.admin list-users
docker compose exec app python -m gaia.core.admin deactivate --phone 13055550001
```

`deactivate` is immediate revocation — the response to a lost or stolen phone,
which is the standing risk when a phone number is the credential.

## Upgrades

`pgvector/pgvector:pg17` is pinned deliberately. A major-version bump makes the
existing data directory unreadable and Postgres refuses to start; moving to pg18
means a dump, a fresh volume, and a restore.
````

- [ ] **Step 4: Commit**

```bash
chmod +x deploy/backup.sh deploy/restore.sh
git add deploy docker-compose.yml
git commit -m "feat: backups, restore drill, and deployment runbook

Nightly pg_dump to Spaces with 30-day retention, because DO droplet
backups are weekly. restore.sh exists so the restore is proven before go
live rather than discovered during an incident."
```

---

## Self-Review

**Spec coverage.** Every section maps to a task: §2.1 layout → Tasks 1-15; §3.1 users → Task 4; §3.2 scope → Task 3; §3.3 ownership → Tasks 6, 8, 15; §3.4 inheritance → Tasks 3, 6, 7; §3.5 schema fixes → Tasks 3, 6, 8; §4 registry → Task 9; §5 flow → Task 12; §5.1 turns → Task 12; §5.2 context → Tasks 5, 12; §5.3 model → Task 10; §5.4 window → Tasks 11, 15; §6 jobs → Task 15; §7 CLI → Task 4; §8 deployment → Tasks 2, 12, 16; §9 testing → throughout; §10 all 19 defects → covered.

**Two spec items deliberately not built here.** The `merge-contacts` CLI command from §7 is listed in the spec's own accepted-debt section (§10.1) as a manual cleanup for a problem that only appears once duplicates exist; it is a ten-line addition when needed. Contact `phone`/`email` columns exist in the schema but no tool writes them — the model has no reliable source for them from meeting notes, and inventing one would be worse than leaving them null.

**Type consistency.** `User` is constructed only in `users._row_to_user`. Handler signature is `(conn, user, args)` everywhere — registry, both capabilities, and the tests. `visible(alias)` always pairs with a `scope_user_id` parameter. `send_text`/`send_template` names match between `WhatsAppClient` and `FakeWhatsApp`.
