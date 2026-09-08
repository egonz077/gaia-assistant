from gaia.capabilities.base import Capability, Registry, Tool
from gaia.core.llm import FALLBACK_TEXT, run_agent
from tests.fakes import FakeAnthropic, FakeResponse, TextBlock, ToolUseBlock


async def test_returns_text_when_the_model_stops(ana):
    client = FakeAnthropic([FakeResponse([TextBlock("Got it — filed.")])])
    out = await run_agent(client, None, ana, [{"role": "user", "content": "hi"}], "sys", [])
    assert out == "Got it — filed."


async def test_runs_a_tool_then_replies(ana):
    calls = []

    async def handler(conn, user, args):
        calls.append(args)
        return {"saved": True}

    reg = Registry()
    reg.register(
        Capability("c", "d", (Tool("save", "Save", {"type": "object", "properties": {}}, handler),))
    )

    client = FakeAnthropic([
        FakeResponse([ToolUseBlock("tu_1", "save", {"summary": "x"})], stop_reason="tool_use"),
        FakeResponse([TextBlock("Filed.")]),
    ])
    out = await run_agent(
        client, None, ana, [{"role": "user", "content": "notes"}], "sys", [], registry=reg
    )
    assert calls == [{"summary": "x"}]
    assert out == "Filed."


async def test_empty_response_falls_back(ana):
    client = FakeAnthropic([FakeResponse([])])
    out = await run_agent(client, None, ana, [{"role": "user", "content": "hi"}], "sys", [])
    assert out == FALLBACK_TEXT


async def test_refusal_falls_back(ana):
    client = FakeAnthropic([FakeResponse([], stop_reason="refusal")])
    out = await run_agent(client, None, ana, [{"role": "user", "content": "hi"}], "sys", [])
    assert out == FALLBACK_TEXT


async def test_iteration_cap_terminates(ana):
    async def handler(conn, user, args):
        return {"ok": True}

    reg = Registry()
    reg.register(
        Capability("c", "d", (Tool("loop", "Loop", {"type": "object", "properties": {}}, handler),))
    )
    client = FakeAnthropic(
        [FakeResponse([ToolUseBlock(f"tu_{i}", "loop", {})], stop_reason="tool_use")
         for i in range(20)]
    )
    out = await run_agent(
        client, None, ana, [{"role": "user", "content": "go"}], "sys", [], registry=reg
    )
    assert out == FALLBACK_TEXT
    assert len(client.requests) == 8   # MAX_ITERATIONS


async def test_request_carries_the_configured_model_and_caching(ana):
    client = FakeAnthropic([FakeResponse([TextBlock("ok")])])
    await run_agent(client, None, ana, [{"role": "user", "content": "hi"}], "sys", [])
    req = client.requests[0]
    assert req["model"] == "claude-opus-5"
    assert req["max_tokens"] == 8000
    assert req["output_config"] == {"effort": "low"}
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
