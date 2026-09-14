from dataclasses import dataclass
from uuid import uuid4

import pytest_asyncio
from psycopg.rows import dict_row

from gaia.core import usage as usage_mod
from gaia.core.db import users as users_db
from gaia.core.db.pool import tx


@pytest_asyncio.fixture
async def ana(migrated):
    """A *committed* user row, unlike the hermetic suite's shared `ana`.

    record() opens its own transaction on another pooled connection, by
    design — telemetry must not join the caller's transaction and be rolled
    back with it. A row left uncommitted in the test's own session is
    invisible from there, so the insert fails the foreign key and record()
    swallows it, exactly as it swallows any other database error. The result
    is a test that silently asserts nothing. tests/live/conftest.py and
    tests/test_webhook.py's wa_user fixture exist for the same reason.
    """
    async with tx(migrated) as conn:
        return await users_db.create_user(conn, name="Ana", wa_id="13055550001")


async def _rows(pool, sql: str) -> list[dict]:
    """Read back as dicts, explicitly.

    Not left to the pool: tx() sets row_factory on the connection it borrows,
    and that setting rides back into the pool with it — so whether a bare
    pool.connection() yields tuples or dicts depends on which connection you
    happen to get. Asserting on positional tuples here passes or fails by
    luck.
    """
    async with pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(sql)
        return await cur.fetchall()


@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 20
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


async def test_one_call_writes_one_row(migrated, ana):
    await usage_mod.record(
        migrated, job="turn", user=ana, model="claude-opus-5",
        usage=FakeUsage(input_tokens=3100, output_tokens=280,
                        cache_read_input_tokens=2900),
        stop_reason="end_turn", duration_ms=1234,
    )

    rows = await _rows(migrated, "SELECT * FROM model_calls")

    assert len(rows) == 1
    assert rows[0]["job"] == "turn"
    assert rows[0]["user_id"] == ana.id
    assert rows[0]["model"] == "claude-opus-5"
    assert rows[0]["input_tokens"] == 3100
    assert rows[0]["output_tokens"] == 280
    assert rows[0]["cache_read_input_tokens"] == 2900
    assert rows[0]["stop_reason"] == "end_turn"
    assert rows[0]["duration_ms"] == 1234


async def test_a_job_with_no_user_is_allowed(migrated):
    """Consolidation runs for nobody. A NULL user_id is a real value here,
    not a missing one."""
    await usage_mod.record(
        migrated, job="consolidation", user=None, model="claude-sonnet-5",
        usage=FakeUsage(), stop_reason="end_turn", duration_ms=10,
    )

    row = (await _rows(migrated, "SELECT job, user_id FROM model_calls"))[0]
    assert row["job"] == "consolidation"
    assert row["user_id"] is None


async def test_a_usage_object_without_cache_fields_records_zeros(migrated, ana):
    """Not every response carries the cache counters. Absent means zero, and
    must not mean a crash inside a turn."""
    @dataclass
    class Bare:
        input_tokens: int = 7
        output_tokens: int = 3

    await usage_mod.record(
        migrated, job="turn", user=ana, model="claude-opus-5", usage=Bare(),
        stop_reason=None, duration_ms=None,
    )

    row = (await _rows(migrated, "SELECT * FROM model_calls"))[0]
    assert row["cache_creation_input_tokens"] == 0
    assert row["cache_read_input_tokens"] == 0
    assert row["stop_reason"] is None
    assert row["duration_ms"] is None


async def test_a_turn_id_is_stored_when_given(migrated, ana):
    """The column the calls-per-turn distribution is grouped by."""
    turn = uuid4()
    await usage_mod.record(
        migrated, job="turn", user=ana, model="claude-opus-5", usage=FakeUsage(),
        stop_reason="end_turn", duration_ms=1, turn_id=turn,
    )

    assert (await _rows(migrated, "SELECT turn_id FROM model_calls"))[0]["turn_id"] == turn


async def test_a_database_failure_is_swallowed(migrated, ana):
    """Telemetry must never cost a reply. The same rule the typing indicator
    follows in butler.py, for the same reason: a metrics insert failing
    during a turn degrades to a missing row, not to an apology."""
    await migrated.close()  # every connection attempt now raises

    await usage_mod.record(
        migrated, job="turn", user=ana, model="claude-opus-5",
        usage=FakeUsage(), stop_reason="end_turn", duration_ms=1,
    )  # must return normally


async def test_a_garbage_usage_object_is_swallowed(migrated, ana):
    """Defensive for the same reason: whatever the SDK hands us, a reply must
    still go out."""
    await usage_mod.record(
        migrated, job="turn", user=ana, model="claude-opus-5",
        usage=object(), stop_reason=None, duration_ms=None,
    )

    assert await _rows(migrated, "SELECT * FROM model_calls") == []
