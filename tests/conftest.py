import os

# MUST come before any gaia import: gaia.core.config instantiates Settings()
# at import time, by design, so a missing variable fails at startup.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")
os.environ.setdefault("WA_ACCESS_TOKEN", "test-token")
os.environ.setdefault("WA_APP_SECRET", "test-secret")
os.environ.setdefault("WA_VERIFY_TOKEN", "test-verify")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("DATABASE_URL", "postgresql://placeholder/overridden_by_pool_fixture")
# Pinned so a developer's real .env cannot steer the suite. Settings reads
# env_file=".env", and real env vars take precedence over that file.
os.environ.setdefault("MODEL", "claude-opus-5")

import psycopg
import pytest
import pytest_asyncio
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from gaia.core.db import users as users_db
from gaia.core.db.migrate import run_migrations

# The compose `db` service — pgvector/pgvector:pg17, the exact production image.
# Start it with: docker compose up -d db
ADMIN_DSN = os.environ.get(
    "TEST_ADMIN_DSN", "postgresql://gaia:devpassword@127.0.0.1:5432/gaia"
)


@pytest.fixture(scope="session")
def pg_admin_dsn():
    """DSN of a database we can connect to in order to create/drop the test one.

    Drops this process's test database on the way out so repeated runs do not
    leave one behind per PID.
    """
    yield ADMIN_DSN
    try:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
    except Exception:  # teardown must never fail a green run
        pass


# Per-process database name. Two pytest runs in parallel (concurrent agents,
# pytest-xdist) would otherwise DROP each other's database mid-test and produce
# failures that look like real bugs.
TEST_DB = f"gaia_test_{os.getpid()}"


@pytest_asyncio.fixture
async def pool(pg_admin_dsn):
    """A pool against a freshly dropped-and-recreated test database."""
    target = pg_admin_dsn.rsplit("/", 1)[0] + f"/{TEST_DB}"

    async with await psycopg.AsyncConnection.connect(
        pg_admin_dsn, autocommit=True
    ) as c:
        await c.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        await c.execute(f'CREATE DATABASE "{TEST_DB}"')

    p = AsyncConnectionPool(target, min_size=1, max_size=4, open=False)
    await p.open(wait=True)
    yield p
    await p.close()


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


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch):
    """Deterministic embeddings. Tests assert on scoping, not on similarity —
    real Voyage calls would make the suite slow, flaky and billable.

    Vectors are sized from EMBED_DIM rather than a literal so a change to the
    real model's dimension shows up here too, instead of only at Postgres
    insert time in production."""
    from gaia.core.embeddings import EMBED_DIM

    async def _embed(texts, input_type="document"):
        return [[float(len(t) % 7)] + [0.0] * (EMBED_DIM - 1) for t in texts]

    monkeypatch.setattr("gaia.core.embeddings.embed", _embed)
    monkeypatch.setattr("gaia.core.db.memory.embed", _embed)
