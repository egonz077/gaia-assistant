from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name       TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def run_migrations(pool) -> list[str]:
    """Apply pending migrations in filename order. Returns what was applied."""
    applied: list[str] = []
    async with pool.connection() as conn:
        # Serialise across replicas; harmless with one.
        await conn.execute("SELECT pg_advisory_lock(hashtext('gaia_migrations'))")
        try:
            await conn.execute(_CREATE_TABLE)
            await conn.commit()

            cur = await conn.execute("SELECT name FROM schema_migrations")
            done = {r[0] for r in await cur.fetchall()}

            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in done:
                    continue
                await conn.execute(path.read_text())
                await conn.execute(
                    "INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,)
                )
                await conn.commit()
                applied.append(path.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtext('gaia_migrations'))")
    return applied
