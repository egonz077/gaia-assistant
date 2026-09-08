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

import pathlib
import psycopg
import pytest
import pytest_asyncio
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from gaia.core.db import users as users_db
from gaia.core.db.migrate import run_migrations

PGDATA = pathlib.Path("/tmp/claude-1000/gaia_pgdata")


@pytest.fixture(scope="session")
def pg_uri():
    """One pgserver instance for the whole session."""
    import pgserver
    PGDATA.mkdir(parents=True, exist_ok=True)
    return pgserver.get_server(str(PGDATA)).get_uri()


@pytest_asyncio.fixture
async def pool(pg_uri):
    """A pool against a freshly dropped-and-recreated gaia_test database."""
    base, _, qs = pg_uri.partition("?")
    root = base.rsplit("/", 1)[0]
    admin, target = f"{root}/postgres?{qs}", f"{root}/gaia_test?{qs}"

    async with await psycopg.AsyncConnection.connect(admin, autocommit=True) as c:
        await c.execute("DROP DATABASE IF EXISTS gaia_test WITH (FORCE)")
        await c.execute("CREATE DATABASE gaia_test")

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
