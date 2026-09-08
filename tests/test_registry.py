import json

import pytest

from gaia.capabilities.base import Capability, Registry, Tool


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
