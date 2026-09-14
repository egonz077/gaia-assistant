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


async def test_llm_calls_has_no_visibility_column(pool):
    """Telemetry is the one table anyone may read: it holds counts and no
    text, which is what makes the report safe to screenshot. A visibility
    column would also break test_scope.py's tripwire, which asserts that the
    set of tables carrying one equals DOMAIN_TABLES exactly."""
    await run_migrations(pool)
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'llm_calls'"""
        )
        columns = {r[0] for r in await cur.fetchall()}

    assert columns == {
        "id", "job", "user_id", "model", "input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
        "stop_reason", "duration_ms", "turn_id", "created_at",
    }
    assert "visibility" not in columns


async def test_deleting_a_user_keeps_their_telemetry(pool):
    """ON DELETE SET NULL, not the RESTRICT the domain tables use. A roster
    change must not erase what the product cost to run last month."""
    await run_migrations(pool)
    async with pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO users (name, wa_id, timezone) VALUES ('Ana','1','UTC')
               RETURNING id"""
        )
        uid = (await cur.fetchone())[0]
        await conn.execute(
            """INSERT INTO llm_calls (job, user_id, model, input_tokens, output_tokens)
               VALUES ('turn', %s, 'claude-opus-5', 10, 5)""",
            (uid,),
        )
        await conn.execute("DELETE FROM users WHERE id = %s", (uid,))
        cur = await conn.execute("SELECT user_id FROM llm_calls")
        assert (await cur.fetchone())[0] is None
