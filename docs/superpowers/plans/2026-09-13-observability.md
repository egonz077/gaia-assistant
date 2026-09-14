# Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every model call's token usage to Postgres, and add `python -m gaia.core.admin stats` to report what the product costs to run and whether prompt caching works.

**Architecture:** One append-only `llm_calls` table holding counts and no text. A `record()` helper writes one row per model call from two call sites (the agent loop, once per iteration; the digest composer, once). Cost is never stored — it is computed at read time from a rates dict, so old rows reprice correctly when Anthropic's prices change. Reporting splits in two: `stats.py` turns the database into a dict of numbers, `stats_html.py` turns that dict into a self-contained page. Neither knows about the other's concerns.

**Tech Stack:** Python 3.11, psycopg 3 (async), Postgres 17, argparse, pytest. No new dependencies — the HTML is hand-rolled with inline CSS and inline SVG.

**Spec:** `docs/superpowers/specs/2026-09-12-observability-design.md`

## Global Constraints

- **No text, ever.** `llm_calls` stores counts only — never prompt or completion text (spec §2). The HTML report carries aggregates only: no contact names, no meeting text, no message content (spec §5.3).
- **`llm_calls` has no `visibility` column.** Deliberate (spec §2). `tests/test_scope.py::test_every_visibility_table_is_registered` asserts that the set of tables carrying a `visibility` column equals `DOMAIN_TABLES` exactly — adding one to `llm_calls` breaks the build and is wrong anyway. Do not add it, and do not add `llm_calls` to `DOMAIN_TABLES`.
- **Telemetry must never cost a reply.** `record()` swallows every exception (spec §3), the same rule `butler.py`'s typing indicator follows.
- **Cost is computed at read time, never stored** (spec §4). An unknown model id reports tokens with no cost and says so — it never guesses.
- **`user_id` is `ON DELETE SET NULL`**, not the `RESTRICT` the domain tables use (spec §2). Telemetry outlives a roster change.
- **Rates are USD per million tokens, Anthropic first-party:** `claude-opus-5` $5.00/$25.00, `claude-sonnet-5` $2.00/$10.00, `claude-haiku-4-5` $1.00/$5.00. Cache write multiplier 1.25, cache read multiplier 0.10.
- **Migrations are applied in filename order** by `gaia/core/db/migrate.py`; the next free number is `003`.
- **Every new db function takes `(conn, user, ...)` or is exempt.** `tests/test_db_signatures.py` enforces the shape for everything under `gaia/core/db/`. Both new modules live at `gaia/core/`, not `gaia/core/db/`, so they are outside that tripwire.

---

### Task 1: The `llm_calls` table

**Files:**
- Create: `migrations/003_llm_calls.sql`
- Test: `tests/test_migrate.py` (add one test)

**Interfaces:**
- Consumes: nothing.
- Produces: table `llm_calls` with columns `id, job, user_id, model, input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, stop_reason, duration_ms, turn_id, created_at`.

**Deviation from the spec, deliberate:** `turn_id` is not in spec §2. It is added because spec §5.1 asks the report to show "calls per turn — the distribution", and the spec's own schema gives no way to tell which rows belong to the same turn. One nullable UUID, generated once per `run_agent` call and shared by that turn's iterations, is the cheapest thing that makes the requested metric computable. It is NULL for single-call jobs, where the notion has no meaning.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_migrate.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_migrate.py -q`
Expected: FAIL — `relation "llm_calls" does not exist`.

- [ ] **Step 3: Write the migration**

Create `migrations/003_llm_calls.sql`:

```sql
-- One row per model call. Counts only, never prompt or completion text:
-- this is the one table in the schema that can be read by anyone without
-- exposing a client, which is what makes the report safe to share.
--
-- No `visibility` column, deliberately. tests/test_scope.py asserts that the
-- set of tables carrying one equals DOMAIN_TABLES exactly.
CREATE TABLE llm_calls (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job          TEXT NOT NULL,          -- 'turn' | 'digest' | 'consolidation'
    -- SET NULL, not the RESTRICT the domain tables use: telemetry should
    -- outlive a roster change, and it carries nothing worth protecting.
    -- Nullable also because consolidation runs for no user at all.
    user_id      UUID REFERENCES users(id) ON DELETE SET NULL,
    model        TEXT NOT NULL,
    input_tokens                INT NOT NULL,
    output_tokens               INT NOT NULL,
    cache_creation_input_tokens INT NOT NULL DEFAULT 0,
    cache_read_input_tokens     INT NOT NULL DEFAULT 0,
    stop_reason  TEXT,
    duration_ms  INT,
    -- NOT in the spec, and required by it: §5.1 asks for "calls per turn --
    -- the distribution, since 8 is the cap and a turn hitting it returns
    -- FALLBACK_TEXT", and the spec's own schema has no way to group the calls
    -- of one turn. Generated per turn by run_agent, shared by that turn's
    -- iterations. NULL for single-call jobs, where it would mean nothing.
    turn_id      UUID,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_llm_calls_day ON llm_calls (created_at DESC);
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_migrate.py tests/test_scope.py -q`
Expected: PASS. `test_scope.py` must stay green — it proves the new table did not acquire a `visibility` column.

- [ ] **Step 5: Commit**

```bash
git add migrations/003_llm_calls.sql tests/test_migrate.py
git commit -m "feat: llm_calls, one row per model call, counts and no text"
```

---

### Task 2: `usage.record()`

**Files:**
- Create: `gaia/core/usage.py`
- Test: `tests/test_usage.py`

**Interfaces:**
- Consumes: the `llm_calls` table from Task 1.
- Produces: `async def record(pool, *, job: str, user: User | None, model: str, usage, stop_reason: str | None, duration_ms: int | None, turn_id=None) -> None`. `usage` is anything exposing `input_tokens` / `output_tokens` and optionally `cache_creation_input_tokens` / `cache_read_input_tokens` — the Anthropic SDK's usage object in production. Returns `None` always and raises nothing, ever.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_usage.py`:

```python
from dataclasses import dataclass

from gaia.core import usage as usage_mod


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

    async with migrated.connection() as conn:
        cur = await conn.execute(
            """SELECT job, user_id, model, input_tokens, output_tokens,
                      cache_read_input_tokens, stop_reason, duration_ms
               FROM llm_calls"""
        )
        rows = await cur.fetchall()

    assert len(rows) == 1
    assert rows[0] == ("turn", ana.id, "claude-opus-5", 3100, 280, 2900, "end_turn", 1234)


async def test_a_job_with_no_user_is_allowed(migrated):
    """Consolidation runs for nobody. A NULL user_id is a real value here,
    not a missing one."""
    await usage_mod.record(
        migrated, job="consolidation", user=None, model="claude-sonnet-5",
        usage=FakeUsage(), stop_reason="end_turn", duration_ms=10,
    )

    async with migrated.connection() as conn:
        cur = await conn.execute("SELECT job, user_id FROM llm_calls")
        assert await cur.fetchone() == ("consolidation", None)


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

    async with migrated.connection() as conn:
        cur = await conn.execute(
            """SELECT cache_creation_input_tokens, cache_read_input_tokens,
                      stop_reason, duration_ms FROM llm_calls"""
        )
        assert await cur.fetchone() == (0, 0, None, None)


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

    async with migrated.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM llm_calls")
        assert (await cur.fetchone())[0] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_usage.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia.core.usage'`.

- [ ] **Step 3: Write the implementation**

Create `gaia/core/usage.py`:

```python
"""Token accounting. Counts only — this module never sees prompt or
completion text, and the table it writes to has no column to put it in."""

import logging

from gaia.core.db.pool import tx
from gaia.core.models import User

log = logging.getLogger("gaia.usage")


async def record(
    pool,
    *,
    job: str,
    user: User | None,
    model: str,
    usage,
    stop_reason: str | None,
    duration_ms: int | None,
    turn_id=None,
) -> None:
    """Write one row for one model call. Never raises.

    Telemetry must never cost a reply. `butler.py` already applies this rule
    to the typing indicator, for the same reason: a metrics insert failing
    part-way through a turn must degrade to a missing row, not to an apology
    for a turn that otherwise worked. Every exception is logged and
    swallowed — including a malformed usage object, not just a database
    failure, since what the SDK hands us is not this module's to guarantee.

    Awaited rather than fired-and-forgotten: it is one small insert against a
    pooled connection, next to a model call that just took seconds. A
    background task would buy nothing and make tests non-deterministic.
    """
    try:
        async with tx(pool) as conn:
            await conn.execute(
                """INSERT INTO llm_calls
                       (job, user_id, model, input_tokens, output_tokens,
                        cache_creation_input_tokens, cache_read_input_tokens,
                        stop_reason, duration_ms, turn_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    job,
                    user.id if user else None,
                    model,
                    usage.input_tokens,
                    usage.output_tokens,
                    getattr(usage, "cache_creation_input_tokens", 0) or 0,
                    getattr(usage, "cache_read_input_tokens", 0) or 0,
                    stop_reason,
                    duration_ms,
                    turn_id,
                ),
            )
    except Exception:
        log.exception("could not record usage for job %s", job)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_usage.py -q`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add gaia/core/usage.py tests/test_usage.py
git commit -m "feat: record(), which writes one usage row and never raises"
```

---

### Task 3: Record every agent-loop iteration

**Files:**
- Modify: `gaia/core/llm.py` (the loop in `run_agent`)
- Modify: `tests/fakes.py` (`FakeResponse` gains `usage`)
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `usage.record` from Task 2.
- Produces: one `llm_calls` row per loop iteration, `job='turn'`.

**Critical:** `FakeResponse` currently has no `usage` attribute. Reading `response.usage` in `run_agent` would raise `AttributeError` in every hermetic test that drives the agent loop. Give `FakeResponse` a default `usage` in the same task — production responses always carry one.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_llm.py`:

```python
async def test_each_iteration_of_a_turn_is_recorded(pool, ana, migrated):
    """One row per model call, not per turn. A turn makes up to
    MAX_ITERATIONS calls and the per-iteration breakdown is the only view
    that shows cache behaviour: the first call pays for cache creation, the
    later ones should read."""
    client = FakeAnthropic([
        FakeResponse([ToolUseBlock("t1", "unknown_tool", {})], stop_reason="tool_use"),
        FakeResponse([TextBlock("done")]),
    ])

    await run_agent(client, migrated, ana, [{"role": "user", "content": "hi"}], "sys", [])

    async with migrated.connection() as conn:
        cur = await conn.execute("SELECT job, user_id, model FROM llm_calls")
        rows = await cur.fetchall()

    assert len(rows) == 2, "two model calls must produce two rows"
    assert all(r[0] == "turn" and r[1] == ana.id for r in rows)


async def test_a_recording_failure_does_not_cost_the_reply(pool, ana, migrated, monkeypatch):
    """The rule from usage.record, asserted at the call site: if telemetry is
    broken the user still gets their answer."""
    async def boom(*a, **kw):
        raise RuntimeError("telemetry is down")

    monkeypatch.setattr("gaia.core.llm.usage.record", boom)
    client = FakeAnthropic([FakeResponse([TextBlock("still answered")])])

    reply = await run_agent(
        client, migrated, ana, [{"role": "user", "content": "hi"}], "sys", []
    )

    assert reply == "still answered"
```

Note: `test_a_recording_failure_does_not_cost_the_reply` monkeypatches `record` to raise, which proves the *call site* is guarded rather than relying on `record`'s own try/except. Both layers matter — see `superpowers:systematic-debugging` → `defense-in-depth.md`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_llm.py -q`
Expected: FAIL — no `llm_calls` rows are written (0 != 2), and an `AttributeError` on `response.usage` once the implementation lands without the `FakeResponse` change.

- [ ] **Step 3: Give `FakeResponse` a usage object**

In `tests/fakes.py`, add above `FakeResponse`:

```python
@dataclass
class FakeUsage:
    """Every real response carries usage. The fake must too, or wiring
    telemetry into the agent loop breaks every test that drives it."""
    input_tokens: int = 100
    output_tokens: int = 20
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
```

and change `FakeResponse` to:

```python
@dataclass
class FakeResponse:
    content: list
    stop_reason: str = "end_turn"
    usage: FakeUsage = field(default_factory=FakeUsage)
```

`field` is already imported at the top of `tests/fakes.py`.

- [ ] **Step 4: Wire the call site**

In `gaia/core/llm.py`, add to the imports:

```python
import time
from uuid import uuid4

from gaia.core import usage as usage_mod
```

Inside `run_agent`, alongside `messages = list(messages)`, add:

```python
    # One id per turn, shared by every iteration of this loop. Spec §5.1 wants
    # the calls-per-turn distribution and nothing else in the row can group
    # them: MAX_ITERATIONS is 8 and a turn that hits the cap returns
    # FALLBACK_TEXT, which is exactly the case worth being able to count.
    turn_id = uuid4()
```

Replace the `response = await client.messages.create(...)` call inside the loop with:

```python
        started = time.monotonic()
        response = await client.messages.create(
            model=settings.model,
            max_tokens=MAX_TOKENS,
            output_config={"effort": "low"},
            system=system_blocks,
            tools=tool_defs,
            messages=messages,
        )
        # Telemetry is not allowed to cost a reply, so it is guarded here as
        # well as inside record() — a turn that answered correctly must not
        # become an apology because a metrics insert failed.
        try:
            await usage_mod.record(
                pool, job="turn", user=user, model=settings.model,
                usage=response.usage, stop_reason=response.stop_reason,
                duration_ms=int((time.monotonic() - started) * 1000),
                turn_id=turn_id,
            )
        except Exception:
            log.exception("could not record usage for a turn")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_llm.py tests/test_butler.py tests/test_turns.py -q`
Expected: PASS. These three exercise the agent loop hardest; if `FakeResponse.usage` were missing they would fail loudly.

- [ ] **Step 6: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, no regressions.

- [ ] **Step 7: Commit**

```bash
git add gaia/core/llm.py tests/fakes.py tests/test_llm.py
git commit -m "feat: record one usage row per agent-loop iteration"
```

---

### Task 4: Record the digest composer

**Files:**
- Modify: `gaia/jobs/digest.py` (`compose_digest`, and `send_digest` which calls it)
- Test: `tests/test_digest.py`

**Interfaces:**
- Consumes: `usage.record` from Task 2.
- Produces: one `llm_calls` row per digest composed, `job='digest'`, `user_id` = the recipient.

**Note on plumbing:** `compose_digest(client, user, leads, commitments)` has no pool, and `send_digest(conn, client, wa, user)` has a *connection*, not a pool. Both gain a **required** `pool` parameter, threaded from `run_once`, which already holds one.

**Required, not optional, and no environment flag.** An earlier draft of this task gave `compose_digest` a `pool=None` default that recorded nothing when omitted. The only caller that would ever omit it is a test — which makes it test-awareness wearing a parameter's clothes. A `DISABLE_TELEMETRY` env var is the same mistake with worse consequences: it puts a switch in production whose failure mode is the silent absence of the data this whole feature exists to collect, and nobody would notice for weeks.

A parameter passed by every caller is a seam, not a test hook: production passes the real pool, a test passes its own, and neither knows the other exists. Tests that need no telemetry monkeypatch `usage.record` (Task 3 does exactly this) or simply let the rows land — every test gets a freshly dropped-and-recreated database, so they vanish with it.

This means updating the existing `send_digest` call sites in `tests/test_digest.py` to pass `migrated`. A test can request both `conn` and `migrated`; they resolve to the same pool, since `conn` is built from it.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_digest.py`:

```python
async def test_composing_a_digest_is_recorded_against_the_recipient(migrated, ana):
    """Per-developer cost is the point of the user_id column, and the digest
    is the one job where a row maps to exactly one person."""
    from gaia.core.config import settings

    async with migrated.connection() as conn:
        conn.row_factory = __import__("psycopg").rows.dict_row
        await users_db.touch_inbound(conn, ana)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        await leads_db.create(
            conn, ana, contact_name="Maria", description="Buying", next_action_at=past
        )
        await conn.commit()

        client = FakeAnthropic([FakeResponse([TextBlock("Morning!")])])
        await digest.send_digest(conn, client, FakeWhatsApp(), ana, pool=migrated)

    async with migrated.connection() as conn:
        cur = await conn.execute("SELECT job, user_id, model FROM llm_calls")
        rows = await cur.fetchall()

    assert rows == [("digest", ana.id, settings.digest_model)]


async def test_a_recording_failure_does_not_cost_the_digest(migrated, ana, monkeypatch):
    """The send matters more than the metric. A telemetry failure at 8am must
    not be the reason nobody gets their morning message."""
    async def boom(*a, **kw):
        raise RuntimeError("telemetry is down")

    monkeypatch.setattr("gaia.jobs.digest.usage_mod.record", boom)

    async with migrated.connection() as conn:
        conn.row_factory = __import__("psycopg").rows.dict_row
        await users_db.touch_inbound(conn, ana)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        await leads_db.create(
            conn, ana, contact_name="Maria", description="Buying", next_action_at=past
        )
        wa = FakeWhatsApp()
        client = FakeAnthropic([FakeResponse([TextBlock("Morning!")])])

        assert await digest.send_digest(conn, client, wa, ana, pool=migrated) is True
        assert len(wa.sent) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_digest.py -q`
Expected: FAIL — `send_digest() got an unexpected keyword argument 'pool'`.

- [ ] **Step 3: Wire the call site**

In `gaia/jobs/digest.py`, add to the imports:

```python
import time

from gaia.core import usage as usage_mod
```

Change `compose_digest`'s signature and its model call:

```python
async def compose_digest(
    client, user: User, leads: list[dict], commitments: list[dict], pool
) -> str:
```

and, replacing the `response = await client.messages.create(...)` call:

```python
    started = time.monotonic()
    response = await client.messages.create(
        model=settings.digest_model,
        max_tokens=2000,
        output_config={"effort": "low"},
        system=SYSTEM,
        messages=[{"role": "user", "content": str(payload)}],
    )
    # `pool` is required and passed by every caller — a seam, not a switch.
    # The guard here matches the agent loop's: a nobody-gets-a-digest failure
    # at 8am is far worse than a missing telemetry row.
    try:
        await usage_mod.record(
            pool, job="digest", user=user, model=settings.digest_model,
            usage=response.usage, stop_reason=response.stop_reason,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception:
        log.exception("could not record usage for a digest")
```

Change `send_digest` to accept and forward the pool:

```python
async def send_digest(conn, client, wa, user: User, pool) -> bool:
```

and its `compose_digest` call:

```python
    text = await compose_digest(client, user, leads, commitments, pool=pool)
```

In `run_once`, forward the pool it already holds:

```python
                if await send_digest(conn, client, wa, user, pool=pool):
```

- [ ] **Step 4: Update the existing call sites**

Every existing `send_digest(conn, client, wa, ana)` call in `tests/test_digest.py` now needs `pool=migrated`, and those tests need `migrated` in their fixture list alongside `conn`. Both resolve to the same pool — `conn` is built from `migrated` — so adding the parameter is the whole change.

Run `grep -n "send_digest(" tests/test_digest.py` to find them all before editing.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_digest.py -q`
Expected: PASS.

- [ ] **Step 6: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, no regressions.

- [ ] **Step 7: Commit**

```bash
git add gaia/jobs/digest.py tests/test_digest.py
git commit -m "feat: record the digest composer's usage against its recipient"
```

---

### Task 5: Cost maths and aggregation

**Files:**
- Create: `gaia/core/stats.py`
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: the `llm_calls` table from Task 1, plus the existing `meetings`, `contacts`, `commitments`, `leads`, `users` tables.
- Produces:
  - `PRICES: dict[str, dict[str, float]]`, `CACHE_WRITE_MULTIPLIER = 1.25`, `CACHE_READ_MULTIPLIER = 0.10`
  - `row_cost(model: str, input_tokens: int, output_tokens: int, cache_creation: int, cache_read: int) -> float | None` — `None` for an unknown model
  - `async def collect(conn, days: int = 30) -> dict` — the full report as plain data

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stats.py`:

```python
from datetime import datetime, timedelta, timezone

from gaia.core import stats
from gaia.core.db import meetings as meetings_db


def test_cost_of_a_known_row_including_cache_multipliers():
    """A fixed token mix produces a fixed dollar figure. Opus 5 at $5/$25 per
    million: 1000 plain input, 1000 cache writes at 1.25x, 10000 cache reads
    at 0.10x, 500 output."""
    cost = stats.row_cost("claude-opus-5", 1000, 500, 1000, 10000)

    expected = (1000 * 5 + 1000 * 5 * 1.25 + 10000 * 5 * 0.10 + 500 * 25) / 1e6
    assert cost == expected
    assert round(cost, 6) == round(0.0295, 6)


def test_an_unknown_model_reports_no_cost_rather_than_guessing():
    """Prices change and models are added. Reporting a number derived from
    the wrong rate card is worse than reporting none."""
    assert stats.row_cost("claude-from-the-future", 1000, 500, 0, 0) is None


def test_every_model_the_app_can_be_configured_with_has_a_price():
    """MODEL and DIGEST_MODEL both default to something in this dict, and
    .env.example offers haiku as a documented option."""
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"):
        assert model in stats.PRICES


async def test_collect_totals_tokens_and_cost_by_job(conn, ana):
    await conn.execute(
        """INSERT INTO llm_calls (job, user_id, model, input_tokens, output_tokens)
           VALUES ('turn', %(u)s, 'claude-opus-5', 1000, 100),
                  ('turn', %(u)s, 'claude-opus-5', 2000, 200),
                  ('digest', %(u)s, 'claude-sonnet-5', 500, 50)""",
        {"u": ana.id},
    )

    data = await stats.collect(conn, days=30)

    by_job = {r["job"]: r for r in data["by_job"]}
    assert by_job["turn"]["calls"] == 2
    assert by_job["turn"]["input_tokens"] == 3000
    assert by_job["digest"]["calls"] == 1
    assert by_job["turn"]["cost"] > by_job["digest"]["cost"]


async def test_collect_reports_the_cache_hit_rate(conn, ana):
    """cache_read / (cache_read + cache_creation + input) — the number that
    settles whether the _system_blocks breakpoint design pays off."""
    await conn.execute(
        """INSERT INTO llm_calls (job, user_id, model, input_tokens, output_tokens,
                                  cache_creation_input_tokens, cache_read_input_tokens)
           VALUES ('turn', %s, 'claude-opus-5', 100, 10, 300, 600)""",
        (ana.id,),
    )

    data = await stats.collect(conn, days=30)

    assert data["cache_hit_rate"] == 0.6


async def test_collect_counts_meetings_and_their_commitments(conn, ana):
    """Product numbers are derived, never stored — counting them into a
    metrics table would duplicate the source of truth and drift from it."""
    await meetings_db.save(
        conn, ana, summary="Coral Gables", source="photo_notes",
        contact_names=["Marta Delgado"],
        commitments=[{"description": "Send comps",
                      "due_at": datetime.now(timezone.utc) + timedelta(days=2)},
                     {"description": "Call the attorney"}],
    )

    data = await stats.collect(conn, days=30)

    assert data["meetings"]["total"] == 1
    assert data["meetings"]["photo"] == 1
    assert data["commitments"]["total"] == 2
    assert data["commitments"]["with_due_date"] == 1


async def test_collect_reports_the_calls_per_turn_distribution(conn, ana):
    """MAX_ITERATIONS is 8 and a turn that hits the cap returns FALLBACK_TEXT,
    so the tail of this distribution is the part worth seeing. Rows with a
    NULL turn_id are single-call jobs, not one-call turns."""
    await conn.execute(
        """INSERT INTO llm_calls (job, user_id, model, input_tokens, output_tokens, turn_id)
           VALUES ('turn', %(u)s, 'claude-opus-5', 1, 1, '11111111-1111-1111-1111-111111111111'),
                  ('turn', %(u)s, 'claude-opus-5', 1, 1, '11111111-1111-1111-1111-111111111111'),
                  ('turn', %(u)s, 'claude-opus-5', 1, 1, '22222222-2222-2222-2222-222222222222'),
                  ('digest', %(u)s, 'claude-sonnet-5', 1, 1, NULL)""",
        {"u": ana.id},
    )

    data = await stats.collect(conn, days=30)

    assert data["calls_per_turn"] == {1: 1, 2: 1}, "the digest row is not a turn"


async def test_collect_reports_contacts_per_meeting(conn, ana):
    await meetings_db.save(
        conn, ana, summary="Two people", source="text",
        contact_names=["Marta Delgado", "Tyler Reed"],
    )
    await meetings_db.save(conn, ana, summary="Nobody named", source="text")

    data = await stats.collect(conn, days=30)

    assert data["contacts_per_meeting"]["max"] == 2
    assert data["contacts_per_meeting"]["mean"] == 1.0


async def test_collect_ignores_rows_outside_the_window(conn, ana):
    await conn.execute(
        """INSERT INTO llm_calls (job, user_id, model, input_tokens, output_tokens, created_at)
           VALUES ('turn', %s, 'claude-opus-5', 1000, 100, now() - interval '60 days')""",
        (ana.id,),
    )

    data = await stats.collect(conn, days=30)

    assert data["totals"]["calls"] == 0


async def test_collect_on_an_empty_database_reports_zeros(conn):
    """A fresh deploy must produce a readable report, not a crash or a
    division by zero."""
    data = await stats.collect(conn, days=30)

    assert data["totals"]["calls"] == 0
    assert data["totals"]["cost"] == 0.0
    assert data["cache_hit_rate"] is None
    assert data["by_job"] == []
    assert data["calls_per_turn"] == {}
    assert data["contacts_per_meeting"]["max"] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_stats.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia.core.stats'`.

- [ ] **Step 3: Write the implementation**

Create `gaia/core/stats.py`:

```python
"""What the product costs to run, and what it produced for the money.

Two kinds of number live here. Product counts are derived on read from the
domain tables — counting them into a metrics table would duplicate the source
of truth and drift from it the first time a row is deleted or merged. Model
counts come from llm_calls, which is the only thing the app genuinely
discards otherwise.

Aggregates only, by construction: nothing in this module selects a contact
name, a meeting summary or a message body, because the report it feeds is
designed to be screenshotted.
"""

# USD per million tokens. Anthropic first-party rates.
#
# Deliberately not stored on the row. Prices change, and storing a computed
# cost means either rewriting history when they do or reporting numbers that
# quietly stop being true. Tokens are the fact; cost is a view over it, so an
# old row reprices correctly.
PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5":   {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}
CACHE_WRITE_MULTIPLIER = 1.25   # cache_creation_input_tokens
CACHE_READ_MULTIPLIER = 0.10    # cache_read_input_tokens


def row_cost(
    model: str, input_tokens: int, output_tokens: int,
    cache_creation: int = 0, cache_read: int = 0,
) -> float | None:
    """Dollars for one call, or None if the model has no rate card.

    None rather than a guess: a number derived from the wrong rate card is
    worse than an admitted gap, because it looks like an answer.
    """
    price = PRICES.get(model)
    if price is None:
        return None
    return (
        input_tokens * price["input"]
        + cache_creation * price["input"] * CACHE_WRITE_MULTIPLIER
        + cache_read * price["input"] * CACHE_READ_MULTIPLIER
        + output_tokens * price["output"]
    ) / 1e6


_CALL_COLUMNS = """model, input_tokens, output_tokens,
                   cache_creation_input_tokens, cache_read_input_tokens"""


def _cost_of(row: dict) -> float:
    return row_cost(
        row["model"], row["input_tokens"], row["output_tokens"],
        row["cache_creation_input_tokens"], row["cache_read_input_tokens"],
    ) or 0.0


async def collect(conn, days: int = 30) -> dict:
    """Every number the report shows, as plain data.

    Returns a dict rather than a dataclass so the renderer stays a pure
    function of data and the tests can assert on it without constructing
    anything.
    """
    window = {"days": days}

    cur = await conn.execute(
        f"""SELECT job, {_CALL_COLUMNS}, stop_reason,
                   created_at::date AS day, user_id
            FROM llm_calls
            WHERE created_at >= now() - make_interval(days => %(days)s)""",
        window,
    )
    calls = await cur.fetchall()

    unknown_models = sorted({c["model"] for c in calls if c["model"] not in PRICES})

    def _group(key):
        out: dict = {}
        for c in calls:
            k = c[key]
            bucket = out.setdefault(k, {
                key: k, "calls": 0, "input_tokens": 0, "output_tokens": 0,
                "cache_creation": 0, "cache_read": 0, "cost": 0.0,
            })
            bucket["calls"] += 1
            bucket["input_tokens"] += c["input_tokens"]
            bucket["output_tokens"] += c["output_tokens"]
            bucket["cache_creation"] += c["cache_creation_input_tokens"]
            bucket["cache_read"] += c["cache_read_input_tokens"]
            bucket["cost"] += _cost_of(c)
        return sorted(out.values(), key=lambda r: -r["cost"])

    cache_read = sum(c["cache_read_input_tokens"] for c in calls)
    cache_write = sum(c["cache_creation_input_tokens"] for c in calls)
    plain_input = sum(c["input_tokens"] for c in calls)
    denominator = cache_read + cache_write + plain_input

    stop_reasons: dict = {}
    for c in calls:
        stop_reasons[c["stop_reason"]] = stop_reasons.get(c["stop_reason"], 0) + 1

    # Names come from users so the per-developer table reads as people rather
    # than uuids. This is the only join to a domain table here, and it takes
    # the name only.
    cur = await conn.execute("SELECT id, name FROM users")
    names = {r["id"]: r["name"] for r in await cur.fetchall()}
    by_user = _group("user_id")
    for row in by_user:
        row["name"] = names.get(row["user_id"], "(deleted or no user)")

    cur = await conn.execute(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE source = 'photo_notes') AS photo,
                  count(*) FILTER (WHERE source = 'text') AS text
           FROM meetings
           WHERE created_at >= now() - make_interval(days => %(days)s)""",
        window,
    )
    meetings = await cur.fetchone()

    cur = await conn.execute(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE due_at IS NOT NULL) AS with_due_date,
                  count(*) FILTER (WHERE done_at IS NOT NULL) AS done
           FROM commitments
           WHERE created_at >= now() - make_interval(days => %(days)s)""",
        window,
    )
    commitments = await cur.fetchone()

    cur = await conn.execute(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE next_action_at IS NOT NULL) AS with_next_action
           FROM leads
           WHERE created_at >= now() - make_interval(days => %(days)s)""",
        window,
    )
    leads = await cur.fetchone()

    cur = await conn.execute(
        """SELECT count(*) AS active,
                  count(*) FILTER (WHERE last_digest_on = (now() at time zone 'utc')::date)
                      AS digested_today
           FROM users WHERE active"""
    )
    users = await cur.fetchone()

    # Spec §5.1: contacts per meeting, mean and max. A meeting with nobody in
    # it usually means the model could not pin a name down, which is worth
    # seeing next to the photo/text split.
    cur = await conn.execute(
        """SELECT coalesce(avg(n), 0)::float AS mean, coalesce(max(n), 0) AS max
           FROM (SELECT count(mc.contact_id) AS n
                 FROM meetings m
                 LEFT JOIN meeting_contacts mc ON mc.meeting_id = m.id
                 WHERE m.created_at >= now() - make_interval(days => %(days)s)
                 GROUP BY m.id) per_meeting""",
        window,
    )
    contacts_per_meeting = await cur.fetchone()

    # Spec §5.1: meetings per day and per developer.
    cur = await conn.execute(
        """SELECT m.created_at::date AS day, u.name, count(*) AS meetings
           FROM meetings m JOIN users u ON u.id = m.user_id
           WHERE m.created_at >= now() - make_interval(days => %(days)s)
           GROUP BY 1, 2 ORDER BY 1""",
        window,
    )
    meetings_by_day_and_user = [dict(r) for r in await cur.fetchall()]

    # Spec §5.1: calls per turn. MAX_ITERATIONS is 8 and a turn that hits the
    # cap returns FALLBACK_TEXT, so the tail of this distribution is the
    # interesting part. turn_id is NULL for single-call jobs, which are not
    # turns and must not be counted as one-call turns.
    cur = await conn.execute(
        """SELECT calls, count(*) AS turns
           FROM (SELECT turn_id, count(*) AS calls FROM llm_calls
                 WHERE turn_id IS NOT NULL
                   AND created_at >= now() - make_interval(days => %(days)s)
                 GROUP BY turn_id) per_turn
           GROUP BY calls ORDER BY calls""",
        window,
    )
    calls_per_turn = {r["calls"]: r["turns"] for r in await cur.fetchall()}

    return {
        "days": days,
        "totals": {
            "calls": len(calls),
            "input_tokens": plain_input,
            "output_tokens": sum(c["output_tokens"] for c in calls),
            "cost": sum(_cost_of(c) for c in calls),
        },
        "by_job": _group("job"),
        "by_model": _group("model"),
        "by_user": by_user,
        "by_day": sorted(_group("day"), key=lambda r: r["day"]),
        "cache_hit_rate": (cache_read / denominator) if denominator else None,
        "stop_reasons": stop_reasons,
        "unknown_models": unknown_models,
        "meetings": dict(meetings),
        "commitments": dict(commitments),
        "leads": dict(leads),
        "users": dict(users),
        "contacts_per_meeting": dict(contacts_per_meeting),
        "meetings_by_day_and_user": meetings_by_day_and_user,
        "calls_per_turn": calls_per_turn,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_stats.py -q`
Expected: PASS, 10 tests.

- [ ] **Step 5: Commit**

```bash
git add gaia/core/stats.py tests/test_stats.py
git commit -m "feat: cost maths and report aggregation, priced at read time"
```

---

### Task 6: `admin stats`, text output

**Files:**
- Modify: `gaia/core/admin.py` (`build_parser`, `_run`)
- Test: `tests/test_admin.py`

**Interfaces:**
- Consumes: `stats.collect` from Task 5.
- Produces: `python -m gaia.core.admin stats [--days 30]` printing a plain-text report to stdout.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_admin.py`:

```python
async def test_stats_prints_totals_and_the_cache_hit_rate(pool, capsys):
    from gaia.core.db.pool import tx
    from gaia.core.db import users as users_db

    await run_migrations(pool)
    async with tx(pool) as conn:
        ana = await users_db.create_user(conn, name="Ana", wa_id="13055550001")
        await conn.execute(
            """INSERT INTO llm_calls (job, user_id, model, input_tokens, output_tokens,
                                      cache_creation_input_tokens, cache_read_input_tokens)
               VALUES ('turn', %s, 'claude-opus-5', 100, 10, 300, 600)""",
            (ana.id,),
        )

    args = build_parser().parse_args(["stats"])
    await _run(args, pool=pool)

    out = capsys.readouterr().out
    assert "turn" in out
    assert "60" in out, "the cache hit rate must appear as a percentage"
    assert "Ana" in out


async def test_stats_on_an_empty_database_says_so(pool, capsys):
    await run_migrations(pool)

    await _run(build_parser().parse_args(["stats"]), pool=pool)

    out = capsys.readouterr().out
    assert "no model calls" in out.lower()
```

Check the existing imports at the top of `tests/test_admin.py` — it already imports `build_parser` and `_run`; add `run_migrations` from `gaia.core.db.migrate` if it is not there.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_admin.py -q`
Expected: FAIL — `argument command: invalid choice: 'stats'`.

- [ ] **Step 3: Add the subcommand**

In `gaia/core/admin.py`, add to `build_parser()` before `return parser`:

```python
    stats_cmd = sub.add_parser(
        "stats", help="what the product cost to run, and what it produced"
    )
    stats_cmd.add_argument("--days", type=int, default=30)
    stats_cmd.add_argument(
        "--html", action="store_true",
        help="write a self-contained page to stdout instead of text",
    )
```

Add to the imports:

```python
from gaia.core import stats as stats_mod
```

Add to the `_run` dispatch chain, after the `merge-contacts` branch:

```python
            elif args.command == "stats":
                data = await stats_mod.collect(conn, days=args.days)
                print(_format_stats(data))
```

And add the formatter above `_run`:

```python
def _format_stats(d: dict) -> str:
    """Plain text, because the common case is reading it over ssh."""
    lines = [f"Last {d['days']} days", ""]

    t = d["totals"]
    if not t["calls"]:
        lines.append("no model calls recorded in this window")
    else:
        lines += [
            f"  {t['calls']} model calls, ${t['cost']:.2f}",
            f"  {t['input_tokens']:,} input tokens, {t['output_tokens']:,} output",
        ]
        if d["cache_hit_rate"] is not None:
            lines.append(f"  cache hit rate: {d['cache_hit_rate'] * 100:.0f}%")
        lines.append("")

        lines.append("By job")
        for r in d["by_job"]:
            lines.append(f"  {r['job']:<14} {r['calls']:>5} calls  ${r['cost']:>8.2f}")
        lines.append("")

        lines.append("By developer")
        for r in d["by_user"]:
            lines.append(f"  {r['name']:<22} {r['calls']:>5} calls  ${r['cost']:>8.2f}")
        lines.append("")

        if d["stop_reasons"]:
            reasons = ", ".join(
                f"{k or 'none'}={v}" for k, v in sorted(
                    d["stop_reasons"].items(), key=lambda kv: str(kv[0])
                )
            )
            lines += [f"Stop reasons: {reasons}", ""]

    if d["unknown_models"]:
        lines += [
            "Tokens counted with no cost — no rate card for: "
            + ", ".join(d["unknown_models"]),
            "",
        ]

    if d["calls_per_turn"]:
        # MAX_ITERATIONS is 8; a turn that hits the cap returned FALLBACK_TEXT.
        spread = ", ".join(f"{k}:{v}" for k, v in sorted(d["calls_per_turn"].items()))
        lines += [f"Calls per turn: {spread}", ""]

    m, c, le = d["meetings"], d["commitments"], d["leads"]
    cpm = d["contacts_per_meeting"]
    lines += [
        "Product",
        f"  meetings filed     {m['total']}  ({m['photo']} photo, {m['text']} text)",
        f"  contacts/meeting   {cpm['mean']:.1f} mean, {cpm['max']} max",
        f"  commitments        {c['total']}  ({c['with_due_date']} dated, {c['done']} done)",
        f"  leads opened       {le['total']}  ({le['with_next_action']} with a next action)",
        f"  active developers  {d['users']['active']}  "
        f"({d['users']['digested_today']} digested today)",
    ]
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_admin.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gaia/core/admin.py tests/test_admin.py
git commit -m "feat: admin stats, the text report"
```

---

### Task 7: `--html`

**Files:**
- Create: `gaia/core/stats_html.py`
- Modify: `gaia/core/admin.py` (the `stats` branch honours `--html`)
- Test: `tests/test_stats_html.py`

**Interfaces:**
- Consumes: the dict from `stats.collect` (Task 5), the `--html` flag already added in Task 6.
- Produces: `render(data: dict) -> str` — one self-contained HTML document.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stats_html.py`:

```python
from gaia.core import stats_html

DATA = {
    "days": 30,
    "totals": {"calls": 12, "input_tokens": 34000, "output_tokens": 2100, "cost": 1.2345},
    "by_job": [{"job": "turn", "calls": 10, "cost": 1.10, "input_tokens": 30000,
                "output_tokens": 2000, "cache_creation": 0, "cache_read": 0}],
    "by_model": [{"model": "claude-opus-5", "calls": 12, "cost": 1.2345,
                  "input_tokens": 34000, "output_tokens": 2100,
                  "cache_creation": 0, "cache_read": 0}],
    "by_user": [{"user_id": None, "name": "Ana", "calls": 12, "cost": 1.2345,
                 "input_tokens": 34000, "output_tokens": 2100,
                 "cache_creation": 0, "cache_read": 0}],
    "by_day": [{"day": "2026-09-12", "calls": 5, "cost": 0.5, "input_tokens": 1,
                "output_tokens": 1, "cache_creation": 0, "cache_read": 0},
               {"day": "2026-09-13", "calls": 7, "cost": 0.73, "input_tokens": 1,
                "output_tokens": 1, "cache_creation": 0, "cache_read": 0}],
    "cache_hit_rate": 0.42,
    "stop_reasons": {"end_turn": 11, "max_tokens": 1},
    "unknown_models": [],
    "meetings": {"total": 3, "photo": 2, "text": 1},
    "commitments": {"total": 8, "with_due_date": 3, "done": 7},
    "leads": {"total": 2, "with_next_action": 2},
    "users": {"active": 2, "digested_today": 2},
    "contacts_per_meeting": {"mean": 1.5, "max": 3},
    "meetings_by_day_and_user": [],
    "calls_per_turn": {1: 4, 2: 3, 8: 1},
}


def test_the_page_carries_the_computed_figures():
    html = stats_html.render(DATA)

    assert "$1.23" in html
    assert "42%" in html
    assert "Ana" in html


def test_the_page_has_no_script_tag():
    """It has to open from a file:// URL on a laptop and a phone, with no CDN
    and no JavaScript."""
    html = stats_html.render(DATA).lower()

    assert "<script" not in html
    assert "http://" not in html
    assert "https://" not in html


def test_the_page_is_a_complete_document():
    html = stats_html.render(DATA)

    assert html.strip().startswith("<!doctype html>")
    assert "</html>" in html


def test_an_empty_window_still_renders():
    empty = {**DATA, "totals": {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                "cost": 0.0},
             "by_job": [], "by_model": [], "by_user": [], "by_day": [],
             "cache_hit_rate": None, "stop_reasons": {}}

    html = stats_html.render(empty)

    assert "</html>" in html
    assert "no model calls" in html.lower()


def test_a_name_with_html_in_it_is_escaped():
    """Developer names come from the roster, which a human typed. The report
    is a file someone opens; it must not execute what the roster says."""
    data = {**DATA, "by_user": [{**DATA["by_user"][0], "name": "<script>x</script>"}]}

    html = stats_html.render(data)

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_stats_html.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia.core.stats_html'`.

- [ ] **Step 3: Write the renderer**

Create `gaia/core/stats_html.py`:

```python
"""One self-contained page: inline CSS, inline SVG, no CDN, no JavaScript.

It has to open from a file:// URL on a laptop and on a phone, which rules out
every external asset. It is also the one artefact of this system designed to
be screenshotted and shared, so it carries aggregates only — no contact
names, no meeting text, ever. The moment it lists a client it becomes another
copy of the client book with worse access control than the database it came
from.
"""

from html import escape

_CSS = """
:root { color-scheme: light dark; }
body { font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       margin: 0; padding: 24px; background: #fbfbf9; color: #1a1a18; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 14px; text-transform: uppercase; letter-spacing: .06em;
     color: #6b6b64; margin: 28px 0 8px; }
.sub { color: #6b6b64; margin: 0 0 24px; }
.big { font-size: 30px; font-weight: 600; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; }
.card { flex: 1 1 150px; background: #fff; border: 1px solid #e6e6e0;
        border-radius: 10px; padding: 14px 16px; }
.card .label { color: #6b6b64; font-size: 13px; }
table { border-collapse: collapse; width: 100%; max-width: 640px; }
td, th { text-align: left; padding: 6px 10px 6px 0; border-bottom: 1px solid #ececE6; }
th { color: #6b6b64; font-weight: 500; font-size: 13px; }
td.n { text-align: right; font-variant-numeric: tabular-nums; }
.note { color: #8a8a80; font-size: 13px; }
@media (prefers-color-scheme: dark) {
  body { background: #17171a; color: #ececE8; }
  .card { background: #202024; border-color: #32323a; }
  td, th { border-color: #2a2a31; }
}
"""


def _bars(by_day: list[dict]) -> str:
    """Inline SVG, one bar per day, scaled to the costliest day."""
    if not by_day:
        return ""
    peak = max(r["cost"] for r in by_day) or 1.0
    width, height, gap = 26, 90, 6
    bars = []
    for i, r in enumerate(by_day):
        h = max(2, round(r["cost"] / peak * (height - 20)))
        x = i * (width + gap)
        bars.append(
            f'<rect x="{x}" y="{height - h}" width="{width}" height="{h}" '
            f'rx="3" fill="#5b7fd4"><title>{escape(str(r["day"]))}: '
            f'${r["cost"]:.2f}</title></rect>'
        )
    total_width = len(by_day) * (width + gap)
    return (
        f'<svg viewBox="0 0 {total_width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="daily cost">{"".join(bars)}</svg>'
    )


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(
            f'<td class="n">{escape(c)}</td>' if i else f"<td>{escape(c)}</td>"
            for i, c in enumerate(r)
        ) + "</tr>"
        for r in rows
    )
    return f"<table><tr>{head}</tr>{body}</table>"


def render(d: dict) -> str:
    t = d["totals"]
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>gaia-butler — last {d['days']} days</title><style>{_CSS}</style>",
        "</head><body>",
        "<h1>gaia-butler</h1>",
        f"<p class='sub'>Last {d['days']} days</p>",
    ]

    if not t["calls"]:
        parts.append("<p>No model calls recorded in this window.</p>")
    else:
        rate = (
            f"{d['cache_hit_rate'] * 100:.0f}%"
            if d["cache_hit_rate"] is not None else "—"
        )
        parts += [
            "<div class='cards'>",
            f"<div class='card'><div class='label'>Cost</div>"
            f"<div class='big'>${t['cost']:.2f}</div></div>",
            f"<div class='card'><div class='label'>Model calls</div>"
            f"<div class='big'>{t['calls']:,}</div></div>",
            f"<div class='card'><div class='label'>Cache hit rate</div>"
            f"<div class='big'>{rate}</div></div>",
            "</div>",
            "<h2>Cost per day</h2>", _bars(d["by_day"]),
            "<h2>By job</h2>",
            _table(["Job", "Calls", "Cost"],
                   [[r["job"], f"{r['calls']:,}", f"${r['cost']:.2f}"]
                    for r in d["by_job"]]),
            "<h2>By model</h2>",
            _table(["Model", "Calls", "Cost"],
                   [[r["model"], f"{r['calls']:,}", f"${r['cost']:.2f}"]
                    for r in d["by_model"]]),
            "<h2>By developer</h2>",
            _table(["Developer", "Calls", "Cost"],
                   [[r["name"], f"{r['calls']:,}", f"${r['cost']:.2f}"]
                    for r in d["by_user"]]),
        ]
        if d["calls_per_turn"]:
            parts += [
                "<h2>Calls per turn</h2>",
                _table(["Calls", "Turns"],
                       [[str(k), f"{v:,}"] for k, v in sorted(d["calls_per_turn"].items())]),
            ]
        if d["stop_reasons"]:
            parts += [
                "<h2>Stop reasons</h2>",
                _table(["Reason", "Calls"],
                       [[str(k or "none"), f"{v:,}"]
                        for k, v in sorted(d["stop_reasons"].items(),
                                           key=lambda kv: str(kv[0]))]),
            ]

    if d["unknown_models"]:
        parts.append(
            "<p class='note'>Tokens counted with no cost — no rate card for: "
            + escape(", ".join(d["unknown_models"])) + "</p>"
        )

    m, c, le = d["meetings"], d["commitments"], d["leads"]
    cpm = d["contacts_per_meeting"]
    parts += [
        "<h2>Product</h2>",
        _table(["", "Count", "Of which"], [
            ["Meetings filed", f"{m['total']:,}", f"{m['photo']} photo / {m['text']} text"],
            ["Contacts per meeting", f"{cpm['mean']:.1f}", f"{cpm['max']} at most"],
            ["Commitments", f"{c['total']:,}", f"{c['with_due_date']} dated / {c['done']} done"],
            ["Leads opened", f"{le['total']:,}", f"{le['with_next_action']} with next action"],
            ["Active developers", f"{d['users']['active']:,}",
             f"{d['users']['digested_today']} digested today"],
        ]),
        # Spec §5.2: the per-developer table is included, and the framing
        # belongs where it will be read. At two people this is curiosity;
        # beyond a handful it can quietly become a performance metric, and
        # someone filing careful photo notes daily costs several times someone
        # who texts occasionally. That is the product working, not waste.
        "<p class='note'>Per-developer cost tracks how much each person used "
        "the assistant, not how well they work.</p>",
        "</body></html>",
    ]
    return "".join(parts)
```

- [ ] **Step 4: Honour the flag in the CLI**

In `gaia/core/admin.py`, add the import:

```python
from gaia.core import stats_html
```

and change the `stats` branch to:

```python
            elif args.command == "stats":
                data = await stats_mod.collect(conn, days=args.days)
                print(stats_html.render(data) if args.html else _format_stats(data))
```

- [ ] **Step 5: Add the CLI test**

Add to `tests/test_admin.py`:

```python
async def test_stats_html_writes_a_page(pool, capsys):
    await run_migrations(pool)

    await _run(build_parser().parse_args(["stats", "--html"]), pool=pool)

    out = capsys.readouterr().out
    assert out.strip().startswith("<!doctype html>")
    assert "<script" not in out.lower()
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_stats_html.py tests/test_admin.py -q`
Expected: PASS.

- [ ] **Step 7: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, no regressions.

- [ ] **Step 8: Document the command in the deploy runbook**

Add to `deploy/README.md`, after the "Adding an agent" section:

```markdown
## What it costs to run

    docker compose exec -T app python -m gaia.core.admin stats
    docker compose exec -T app python -m gaia.core.admin stats --days 7

For a page you can open on a laptop or a phone, pull it down over the ssh
session you already have — no scp step, no new port, nothing listening:

    ssh gaia 'cd /opt/gaia-assistant && docker compose exec -T app \
        python -m gaia.core.admin stats --html' > report.html

The report carries aggregates only — no contact names and no meeting text —
so it is safe to screenshot. Cost is computed when the report is read, from
the rate card in `gaia/core/stats.py`; update that dict when Anthropic's
prices change and every historical row reprices correctly.
```

- [ ] **Step 9: Commit**

```bash
git add gaia/core/stats_html.py gaia/core/admin.py tests/test_stats_html.py tests/test_admin.py deploy/README.md
git commit -m "feat: admin stats --html, a self-contained page"
```

---

## Notes for the executor

- **Do not add a `visibility` column to `llm_calls`**, and do not add it to `DOMAIN_TABLES`. `tests/test_scope.py::test_every_visibility_table_is_registered` asserts those two sets match exactly; `llm_calls` belongs in neither.
- **`tests/factories.py` needs no entry for `llm_calls`.** It builds rows for the parametrized isolation suite, which iterates `DOMAIN_TABLES` only.
- **The live tier (`pytest -m live`) is deselected by default and is billable.** Run it once at the end (`pytest -m live`) to confirm the two real call sites still work against the real API — `eff08ce` is the precedent for a change that left that tier silently red for a day.
- **`consolidation` is a third job in the spec's table but has no call site yet** — its own spec is unimplemented. The `job` column accepts it; nothing needs to write it today.
