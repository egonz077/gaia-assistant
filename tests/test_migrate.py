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
