import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from gaia.core.db.pool import tx


async def test_tx_works_without_caller_opening_the_pool(pool):
    """tx() must open the pool itself; PoolClosed must never surface."""
    fresh = AsyncConnectionPool(pool.conninfo, min_size=1, max_size=4, open=False)
    try:
        async with tx(fresh) as conn:
            await conn.execute("SELECT 1")
    finally:
        await fresh.close()


async def test_tx_yields_dict_row_rows(pool):
    async with tx(pool) as conn:
        await conn.execute("CREATE TABLE t (id INT, name TEXT)")
        await conn.execute("INSERT INTO t (id, name) VALUES (1, 'a')")
        row = await (await conn.execute("SELECT id, name FROM t")).fetchone()
    assert row == {"id": 1, "name": "a"}


async def test_tx_commits_on_clean_exit(pool):
    async with tx(pool) as conn:
        await conn.execute("CREATE TABLE t2 (id INT)")
        await conn.execute("INSERT INTO t2 (id) VALUES (1)")

    async with pool.connection() as conn:
        conn.row_factory = dict_row
        rows = await (await conn.execute("SELECT id FROM t2")).fetchall()
    assert rows == [{"id": 1}]


async def test_tx_rolls_back_on_exception(pool):
    async with pool.connection() as conn:
        await conn.execute("CREATE TABLE t3 (id INT)")
        await conn.commit()

    with pytest.raises(RuntimeError):
        async with tx(pool) as conn:
            await conn.execute("INSERT INTO t3 (id) VALUES (1)")
            raise RuntimeError("boom")

    async with pool.connection() as conn:
        rows = await (await conn.execute("SELECT id FROM t3")).fetchall()
    assert rows == []
