from gaia.core.db.migrate import run_migrations
from gaia.core.db.scope import DOMAIN_TABLES, visible


def test_visible_fragment_uses_named_parameter():
    assert visible("m") == "(m.visibility = 'org' OR m.user_id = %(scope_user_id)s)"


async def test_every_visibility_table_is_registered(pool):
    """A new domain table must be added to DOMAIN_TABLES or the build breaks."""
    await run_migrations(pool)
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT table_name FROM information_schema.columns
               WHERE table_schema = 'public' AND column_name = 'visibility'"""
        )
        found = {r[0] for r in await cur.fetchall()}
    assert found == set(DOMAIN_TABLES), (
        f"tables with a visibility column but not in DOMAIN_TABLES: {found - set(DOMAIN_TABLES)}"
    )
