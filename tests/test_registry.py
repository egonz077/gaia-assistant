import json
from uuid import uuid4

import pytest

from gaia.capabilities.base import Capability, Registry, Tool
from gaia.core.models import User


async def _echo(conn, user, args):
    return {"echoed": args.get("value")}


PUBLIC = Capability(
    name="public_cap",
    description="Everyone",
    tools=(Tool("echo", "Echo a value", {"type": "object", "properties": {}}, _echo),),
)
ADMIN_ONLY = Capability(
    name="admin_cap",
    description="Admins only",
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


async def test_dispatch_refuses_a_tool_from_an_invisible_capability(registry, ana):
    result = await registry.dispatch(None, ana, "secret", {})
    assert "not available" in result


async def test_dispatch_runs_a_visible_tool(registry, ana):
    result = await registry.dispatch(None, ana, "echo", {"value": 42})
    assert json.loads(result) == {"echoed": 42}


async def test_dispatch_reports_an_unknown_tool(registry, ana):
    assert "unknown tool" in await registry.dispatch(None, ana, "nope", {})


async def test_dispatch_does_not_leak_exception_internals_to_the_model(registry, ana):
    async def _boom(conn, user, args):
        raise RuntimeError("SELECT secret_column FROM internal_table WHERE id = 42")

    boom_cap = Capability(
        name="boom_cap",
        description="Always fails",
        tools=(Tool("boom", "Boom", {"type": "object", "properties": {}}, _boom),),
    )
    registry.register(boom_cap)

    result = await registry.dispatch(None, ana, "boom", {})

    assert "secret_column" not in result
    assert "internal_table" not in result
    assert "failed" in result


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
        cap = Capability(name="c", description="d", tools=())
        assert cap.visible_to(_user()) is True

    def test_role_allowlist_matching_role_is_visible(self):
        cap = Capability(
            name="c", description="d", tools=(),
            allowed_roles=frozenset({"admin"}),
        )
        assert cap.visible_to(_user(role="admin")) is True

    def test_role_allowlist_nonmatching_role_is_hidden(self):
        cap = Capability(
            name="c", description="d", tools=(),
            allowed_roles=frozenset({"admin"}),
        )
        assert cap.visible_to(_user(role="agent")) is False

    def test_user_id_allowlist_matching_id_is_visible(self):
        uid = uuid4()
        cap = Capability(
            name="c", description="d", tools=(),
            allowed_user_ids=frozenset({uid}),
        )
        assert cap.visible_to(_user(id=uid, role="agent")) is True

    def test_user_id_allowlist_nonmatching_id_is_hidden(self):
        cap = Capability(
            name="c", description="d", tools=(),
            allowed_user_ids=frozenset({uuid4()}),
        )
        assert cap.visible_to(_user(id=uuid4(), role="agent")) is False

    def test_both_allowlists_set_matching_either_one_is_visible(self):
        uid = uuid4()
        cap = Capability(
            name="c", description="d", tools=(),
            allowed_roles=frozenset({"admin"}),
            allowed_user_ids=frozenset({uid}),
        )
        # Matches only the user_id allowlist, not the role allowlist — an
        # `and`-based implementation would wrongly hide this from the user.
        assert cap.visible_to(_user(id=uid, role="agent")) is True
