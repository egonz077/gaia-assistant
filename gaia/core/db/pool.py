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
