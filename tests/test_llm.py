import json

from gaia.capabilities.base import Capability, Registry, Tool
from gaia.core.llm import FALLBACK_TEXT, run_agent
from tests.fakes import FakeAnthropic, FakeResponse, TextBlock, ToolUseBlock


async def test_returns_text_when_the_model_stops(ana, migrated):
    client = FakeAnthropic([FakeResponse([TextBlock("Got it — filed.")])])
    out = await run_agent(client, migrated, ana, [{"role": "user", "content": "hi"}], "sys", [])
    assert out == "Got it — filed."


async def test_runs_a_tool_then_replies(ana, migrated):
    calls = []

    async def handler(conn, user, args):
        calls.append(args)
        return {"saved": True}

    reg = Registry()
    reg.register(
        Capability("c", (Tool("save", "Save", {"type": "object", "properties": {}}, handler),))
    )

    client = FakeAnthropic([
        FakeResponse([ToolUseBlock("tu_1", "save", {"summary": "x"})], stop_reason="tool_use"),
        FakeResponse([TextBlock("Filed.")]),
    ])
    out = await run_agent(
        client, migrated, ana, [{"role": "user", "content": "notes"}], "sys", [], registry=reg
    )
    assert calls == [{"summary": "x"}]
    assert out == "Filed."


async def test_empty_response_falls_back(ana, migrated):
    client = FakeAnthropic([FakeResponse([])])
    out = await run_agent(client, migrated, ana, [{"role": "user", "content": "hi"}], "sys", [])
    assert out == FALLBACK_TEXT


async def test_refusal_falls_back(ana, migrated):
    client = FakeAnthropic([FakeResponse([], stop_reason="refusal")])
    out = await run_agent(client, migrated, ana, [{"role": "user", "content": "hi"}], "sys", [])
    assert out == FALLBACK_TEXT


async def test_iteration_cap_terminates(ana, migrated):
    async def handler(conn, user, args):
        return {"ok": True}

    reg = Registry()
    reg.register(
        Capability("c", (Tool("loop", "Loop", {"type": "object", "properties": {}}, handler),))
    )
    client = FakeAnthropic(
        [FakeResponse([ToolUseBlock(f"tu_{i}", "loop", {})], stop_reason="tool_use")
         for i in range(20)]
    )
    out = await run_agent(
        client, migrated, ana, [{"role": "user", "content": "go"}], "sys", [], registry=reg
    )
    assert out == FALLBACK_TEXT
    assert len(client.requests) == 8   # MAX_ITERATIONS


async def test_request_carries_the_configured_model_and_caching(ana, migrated):
    client = FakeAnthropic([FakeResponse([TextBlock("ok")])])
    await run_agent(client, migrated, ana, [{"role": "user", "content": "hi"}], "sys", [])
    req = client.requests[0]
    assert req["model"] == "claude-opus-5"
    assert req["max_tokens"] == 8000
    assert req["output_config"] == {"effort": "low"}
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}


async def test_only_the_stable_prefix_is_cached(ana, migrated):
    """Spec §5.3: the breakpoint goes after the stable prefix, not after
    everything. With volatile content inside the cached block, the roster
    reordering on a save_meeting invalidates the system prompt and every tool
    definition with it — cache writes bill at 1.25x, so caching then costs
    more than not caching."""
    client = FakeAnthropic([FakeResponse([TextBlock("ok")])])
    await run_agent(
        client, migrated, ana, [{"role": "user", "content": "hi"}],
        ["stable base prompt", "Today is 2026-09-08. Roster: Delgado, Rivera"], [],
    )
    system = client.requests[0]["system"]

    assert len(system) == 2
    assert system[0]["text"] == "stable base prompt"
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "Roster" not in system[0]["text"]
    assert "cache_control" not in system[1]


async def test_batches_multiple_tool_calls_into_one_message(ana, migrated):
    calls = []

    async def handler_a(conn, user, args):
        calls.append(("a", args))
        return {"which": "alpha"}

    async def handler_b(conn, user, args):
        calls.append(("b", args))
        return {"which": "beta"}

    reg = Registry()
    reg.register(
        Capability(
            "c",
            (
                Tool("tool_a", "A", {"type": "object", "properties": {}}, handler_a),
                Tool("tool_b", "B", {"type": "object", "properties": {}}, handler_b),
            ),
        )
    )

    client = FakeAnthropic(
        [
            FakeResponse(
                [
                    ToolUseBlock("tu_a", "tool_a", {"x": 1}),
                    ToolUseBlock("tu_b", "tool_b", {"y": 2}),
                ],
                stop_reason="tool_use",
            ),
            FakeResponse([TextBlock("Done.")]),
        ]
    )
    messages = [{"role": "user", "content": "go"}]
    out = await run_agent(client, migrated, ana, messages, "sys", [], registry=reg)

    assert calls == [("a", {"x": 1}), ("b", {"y": 2})]
    assert out == "Done."

    sent_on_second_call = client.requests[1]["messages"]
    # original user turn + one assistant turn (both tool_use blocks) + exactly
    # one user turn carrying both tool_results — not two separate messages.
    assert len(sent_on_second_call) == 3
    result_message = sent_on_second_call[2]
    assert result_message["role"] == "user"
    assert len(result_message["content"]) == 2
    results_by_id = {block["tool_use_id"]: block for block in result_message["content"]}
    assert set(results_by_id) == {"tu_a", "tu_b"}
    assert results_by_id["tu_a"]["type"] == "tool_result"
    assert results_by_id["tu_b"]["type"] == "tool_result"
    # Pairing, not just presence: tu_a must carry tool_a's own payload and
    # tu_b must carry tool_b's — a swap of the two contents must fail this.
    assert json.loads(results_by_id["tu_a"]["content"])["which"] == "alpha"
    assert json.loads(results_by_id["tu_b"]["content"])["which"] == "beta"


async def test_whitespace_only_response_falls_back(ana, migrated):
    client = FakeAnthropic([FakeResponse([TextBlock("   \n  ")])])
    out = await run_agent(client, migrated, ana, [{"role": "user", "content": "hi"}], "sys", [])
    assert out == FALLBACK_TEXT


async def test_does_not_mutate_the_callers_messages_list(ana, migrated):
    client = FakeAnthropic([FakeResponse([TextBlock("ok")])])
    messages = [{"role": "user", "content": "hi"}]
    before = list(messages)
    await run_agent(client, migrated, ana, messages, "sys", [])
    assert messages == before


async def test_does_not_mutate_the_callers_messages_list_with_tool_use(ana, migrated):
    async def handler(conn, user, args):
        return {"ok": True}

    reg = Registry()
    reg.register(
        Capability("c", (Tool("save", "Save", {"type": "object", "properties": {}}, handler),))
    )
    client = FakeAnthropic(
        [
            FakeResponse([ToolUseBlock("tu_1", "save", {})], stop_reason="tool_use"),
            FakeResponse([TextBlock("Filed.")]),
        ]
    )
    messages = [{"role": "user", "content": "notes"}]
    before = list(messages)
    await run_agent(client, migrated, ana, messages, "sys", [], registry=reg)
    assert messages == before


async def test_the_agent_loop_uses_the_butlers_model(pool, ana):
    """The other half of the split. The butler transcribes photographed
    handwriting, picks tools and does date arithmetic — it stays on the
    expensive model, and a change to the digest's setting must never drag it
    down with it."""
    from gaia.core.config import settings

    client = FakeAnthropic([FakeResponse([TextBlock("ok")])])

    await run_agent(client, pool, ana, [{"role": "user", "content": "hi"}], "sys", [])

    assert client.requests[0]["model"] == settings.model


async def test_each_iteration_of_a_turn_is_recorded(migrated):
    """One row per model call, not per turn. A turn makes up to
    MAX_ITERATIONS calls, and the per-iteration breakdown is the only view
    that shows cache behaviour: the first call pays for cache creation, the
    later ones should read.

    Uses a committed user for the same reason tests/test_usage.py does —
    record() inserts on another pooled connection and a foreign key failure
    there is swallowed, leaving a test that asserts nothing.
    """
    from psycopg.rows import dict_row

    from gaia.core.db import users as users_db
    from gaia.core.db.pool import tx

    async with tx(migrated) as conn:
        user = await users_db.create_user(conn, name="Ana", wa_id="13055559001")

    client = FakeAnthropic([
        FakeResponse([ToolUseBlock("t1", "nope", {})], stop_reason="tool_use"),
        FakeResponse([TextBlock("done")]),
    ])

    await run_agent(client, migrated, user, [{"role": "user", "content": "hi"}], "sys", [])

    async with migrated.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute("SELECT job, user_id, turn_id FROM llm_calls")
        rows = await cur.fetchall()

    assert len(rows) == 2, "two model calls must produce two rows"
    assert all(r["job"] == "turn" and r["user_id"] == user.id for r in rows)
    assert rows[0]["turn_id"] == rows[1]["turn_id"], (
        "both calls belong to one turn, so the calls-per-turn distribution "
        "must be able to group them"
    )


async def test_a_recording_failure_does_not_cost_the_reply(migrated, ana, monkeypatch):
    """The rule from usage.record, asserted at the call site: if telemetry is
    broken the user still gets their answer. Guarded in both places on
    purpose — record() swallowing its own errors does not protect the turn
    from an exception raised on the way in."""
    async def boom(*a, **kw):
        raise RuntimeError("telemetry is down")

    monkeypatch.setattr("gaia.core.llm.usage_mod.record", boom)
    client = FakeAnthropic([FakeResponse([TextBlock("still answered")])])

    reply = await run_agent(
        client, migrated, ana, [{"role": "user", "content": "hi"}], "sys", []
    )

    assert reply == "still answered"
