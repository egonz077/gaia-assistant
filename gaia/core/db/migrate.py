from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name       TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def run_migrations(pool) -> list[str]:
    """Apply pending migrations in filename order, as a single transaction.

    Guarded by a transaction-scoped advisory lock (pg_advisory_xact_lock):
    serialises concurrent callers, and — because it is tied to the
    transaction rather than the session — is released automatically on
    commit *or* rollback. A failure anywhere in the batch rolls back every
    migration and every schema_migrations row from this call; nothing is
    left half-applied and no lock is left held on the connection when it
    goes back to the pool.
    """
    applied: list[str] = []
    async with pool.connection() as conn:
        # schema_migrations must exist before it can be queried below, and
        # this needs its own transaction: the lock (and the batch it guards)
        # must not depend on this table having just been created.
        await conn.execute(_CREATE_TABLE)
        await conn.commit()

        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('gaia_migrations'))")

            cur = await conn.execute("SELECT name FROM schema_migrations")
            done = {r[0] for r in await cur.fetchall()}

            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in done:
                    continue
                await conn.execute(path.read_text())
                await conn.execute(
                    "INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,)
                )
                applied.append(path.name)
    return applied
