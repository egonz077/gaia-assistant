import json
from uuid import uuid4

import pytest
import pytest_asyncio

from gaia.capabilities.base import Capability, Registry, Tool
from gaia.core.models import User


async def _echo(conn, user, args):
    return {"echoed": args.get("value")}


PUBLIC = Capability(
    name="public_cap",
    tools=(Tool("echo", "Echo a value", {"type": "object", "properties": {}}, _echo),),
)
ADMIN_ONLY = Capability(
    name="admin_cap",
    tools=(Tool("secret", "Secret", {"type": "object", "properties": {}}, _echo),),
    allowed_roles=frozenset({"admin"}),
)


@pytest.fixture
def registry():
    r = Registry()
    r.register(PUBLIC)
    r.register(ADMIN_ONLY)
    return r


def test_public_capability_is_visible_to_everyone(registry, ana):
    assert [c.name for c in registry.for_user(ana)] == ["public_cap"]


def test_restricted_capability_is_absent_from_the_tool_list(registry, ana):
    assert [t["name"] for t in registry.tool_defs(ana)] == ["echo"]


def test_restricted_capability_is_present_for_an_allowed_role(registry, ana):
    admin = type(ana)(**{**ana.__dict__, "role": "admin"})
    assert {t["name"] for t in registry.tool_defs(admin)} == {"echo", "secret"}


async def test_dispatch_refuses_a_tool_from_an_invisible_capability(registry, ana, migrated):
    result = await registry.dispatch(migrated, ana, "secret", {})
    assert "not available" in result


async def test_dispatch_runs_a_visible_tool(registry, ana, migrated):
    result = await registry.dispatch(migrated, ana, "echo", {"value": 42})
    assert json.loads(result) == {"echoed": 42}


async def test_dispatch_reports_an_unknown_tool(registry, ana, migrated):
    assert "unknown tool" in await registry.dispatch(migrated, ana, "nope", {})


async def test_dispatch_does_not_leak_exception_internals_to_the_model(registry, ana, migrated):
    async def _boom(conn, user, args):
        raise RuntimeError("SELECT secret_column FROM internal_table WHERE id = 42")

    boom_cap = Capability(
        name="boom_cap",
        tools=(Tool("boom", "Boom", {"type": "object", "properties": {}}, _boom),),
    )
    registry.register(boom_cap)

    result = await registry.dispatch(migrated, ana, "boom", {})

    assert "secret_column" not in result
    assert "internal_table" not in result
    assert "failed" in result


@pytest_asyncio.fixture
async def committed_ana(migrated):
    """A user other pooled connections can actually see.

    The shared `ana` fixture inserts on the `conn` fixture's connection, inside
    a transaction that is never committed — invisible to the separate
    connection `dispatch` now takes per tool call.
    """
    from gaia.core.db import users as users_db
    from gaia.core.db.pool import tx

    async with tx(migrated) as c:
        return await users_db.create_user(c, name="Ana", wa_id="13055559999")


async def test_a_failing_tool_does_not_destroy_the_turns_earlier_work(
    registry, committed_ana, migrated
):
    """The whole reason dispatch takes a pool and gives each handler its own
    transaction.

    A *database* error is not recoverable by catching it: psycopg leaves the
    connection INERROR, so every later statement raises
    InFailedSqlTransaction. When one transaction spanned the whole turn, a
    model hallucinating a uuid was enough to abort the log of the assistant's
    reply as well — and the rollback took the meeting saved two tool calls
    earlier with it. She photographed her notes, was told something went
    wrong, and the notes were gone.

    So: a real connection, a real SQL error (not a RuntimeError), work
    committed before it, and a further tool call after it.
    """
    from gaia.core.db import meetings as meetings_db
    from gaia.core.db.pool import tx

    async def _file_it(conn, user, args):
        await meetings_db.save(conn, user, summary="Showed Coral Gables", source="text")
        return {"saved": True}

    async def _bad_uuid(conn, user, args):
        # Exactly what update_lead does with an id the model invented.
        await conn.execute("SELECT * FROM leads WHERE id = %s", ("not-a-uuid",))
        return {"unreachable": True}

    registry.register(Capability(
        name="turn_cap",
        tools=(
            Tool("file_it", "File", {"type": "object", "properties": {}}, _file_it),
            Tool("bad_uuid", "Boom", {"type": "object", "properties": {}}, _bad_uuid),
        ),
    ))

    user = committed_ana
    assert json.loads(await registry.dispatch(migrated, user, "file_it", {})) == {"saved": True}

    failure = await registry.dispatch(migrated, user, "bad_uuid", {})
    assert "failed" in failure  # the model is told, and can carry on

    # The loop continues: a later tool call still works on a usable connection.
    assert json.loads(
        await registry.dispatch(migrated, user, "echo", {"value": 7})
    ) == {"echoed": 7}

    # And the earlier work survived the failure.
    async with tx(migrated) as conn:
        cur = await conn.execute("SELECT summary FROM meetings WHERE user_id = %s", (user.id,))
        assert [r["summary"] for r in await cur.fetchall()] == ["Showed Coral Gables"]


def _user(**overrides) -> User:
    defaults = dict(
        id=uuid4(), name="Test", wa_id="15550000000", role="agent",
        timezone="UTC", active=True,
    )
    defaults.update(overrides)
    return User(**defaults)


class TestVisibleTo:
    """Capability.visible_to has 6 meaningful cases; all six covered here
    directly, independent of Registry/DB fixtures — this is pure logic."""

    def test_both_allowlists_none_is_visible_to_anyone(self):
        cap = Capability(name="c", tools=())
        assert cap.visible_to(_user()) is True

    def test_role_allowlist_matching_role_is_visible(self):
        cap = Capability(
            name="c", tools=(),
            allowed_roles=frozenset({"admin"}),
        )
        assert cap.visible_to(_user(role="admin")) is True

    def test_role_allowlist_nonmatching_role_is_hidden(self):
        cap = Capability(
            name="c", tools=(),
            allowed_roles=frozenset({"admin"}),
        )
        assert cap.visible_to(_user(role="agent")) is False

    def test_user_id_allowlist_matching_id_is_visible(self):
        uid = uuid4()
        cap = Capability(
            name="c", tools=(),
            allowed_user_ids=frozenset({uid}),
        )
        assert cap.visible_to(_user(id=uid, role="agent")) is True

    def test_user_id_allowlist_nonmatching_id_is_hidden(self):
        cap = Capability(
            name="c", tools=(),
            allowed_user_ids=frozenset({uuid4()}),
        )
        assert cap.visible_to(_user(id=uuid4(), role="agent")) is False

    def test_both_allowlists_set_matching_either_one_is_visible(self):
        uid = uuid4()
        cap = Capability(
            name="c", tools=(),
            allowed_roles=frozenset({"admin"}),
            allowed_user_ids=frozenset({uid}),
        )
        # Matches only the user_id allowlist, not the role allowlist — an
        # `and`-based implementation would wrongly hide this from the user.
        assert cap.visible_to(_user(id=uid, role="agent")) is True
