import pytest

from gaia.core.db import migrate
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


async def test_failing_migration_rolls_back_batch_and_does_not_leak_the_lock(
    pool, tmp_path, monkeypatch
):
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)
    (tmp_path / "001_ok.sql").write_text("CREATE TABLE ok_table (id INT);")
    (tmp_path / "002_bad.sql").write_text("THIS IS NOT VALID SQL;")

    with pytest.raises(Exception):
        await run_migrations(pool)

    async with pool.connection() as conn:
        rows = await (await conn.execute("SELECT name FROM schema_migrations")).fetchall()
        assert rows == []

        regclass = await (await conn.execute("SELECT to_regclass('ok_table')")).fetchone()
        assert regclass[0] is None

    # Fix the batch: the failed attempt must not have leaked the advisory
    # lock or left anything half-applied that blocks a retry.
    (tmp_path / "002_bad.sql").unlink()
    second = await run_migrations(pool)
    assert second == ["001_ok.sql"]
