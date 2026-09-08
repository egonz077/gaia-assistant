import logging

from gaia.capabilities.base import registry as default_registry
from gaia.core.config import settings
from gaia.core.models import User

log = logging.getLogger("gaia.llm")

MAX_ITERATIONS = 8
MAX_TOKENS = 8000
FALLBACK_TEXT = "Sorry — I couldn't work that one out. Could you try rephrasing?"


async def run_agent(
    client,
    pool,
    user: User,
    messages: list[dict],
    system: str,
    tool_defs: list[dict],
    registry=None,
) -> str:
    """Tool-use loop until the model produces text, or the cap is reached.

    Never returns an empty string: WhatsApp rejects an empty body, and a
    refusal or a max_tokens stop can legitimately produce no text block.

    Takes the connection *pool* rather than a connection. This loop is the
    slow part of a turn — up to MAX_ITERATIONS model calls, 10-30 seconds on
    a photo — and holding one of a handful of pooled connections open, inside
    a transaction, across all of that both starves the pool and forces every
    tool in the turn to share one transaction, where a single failure aborts
    the lot. Registry.dispatch opens its own short transaction per call.

    Does not mutate the caller's `messages` list. The loop works on a local
    copy — assistant turns and tool_result turns accumulated while iterating
    are not written back, so the caller must not rely on this function to
    return or persist conversation state.
    """
    reg = registry or default_registry
    messages = list(messages)
    # The system prompt is stable per user, so it is the cache breakpoint.
    system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    for _ in range(MAX_ITERATIONS):
        response = await client.messages.create(
            model=settings.model,
            max_tokens=MAX_TOKENS,
            output_config={"effort": "low"},
            system=system_blocks,
            tools=tool_defs,
            messages=messages,
        )

        if response.stop_reason == "refusal":
            log.warning("model refused for user %s", user.id)
            return FALLBACK_TEXT

        if response.stop_reason != "tool_use":
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            if response.stop_reason == "max_tokens":
                log.warning("hit max_tokens for user %s", user.id)
            return text or FALLBACK_TEXT

        messages.append({"role": "assistant", "content": response.content})
        # All tool_use blocks from one assistant turn must come back as a
        # single user message with multiple tool_result blocks — splitting
        # them across messages violates the API contract and silently trains
        # the model to stop making parallel tool calls.
        results = []
        for block in response.content:
            if block.type == "tool_use":
                output = await reg.dispatch(pool, user, block.name, block.input)
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": output}
                )
        messages.append({"role": "user", "content": results})

    log.warning("iteration cap reached for user %s", user.id)
    return FALLBACK_TEXT
