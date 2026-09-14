import logging
import time
from collections.abc import Sequence
from uuid import uuid4

from gaia.capabilities.base import registry as default_registry
from gaia.core import usage as usage_mod
from gaia.core.config import settings
from gaia.core.models import User

log = logging.getLogger("gaia.llm")

MAX_ITERATIONS = 8
MAX_TOKENS = 8000
FALLBACK_TEXT = "Sorry — I couldn't work that one out. Could you try rephrasing?"


def _system_blocks(system: str | Sequence[str]) -> list[dict]:
    """Stable prefix first and marked as the cache breakpoint; volatile
    content after it, unmarked.

    Spec §5.3: caching covers the tools plus the base system prefix, which is
    stable per user, and content that changes often goes after the
    breakpoint. Marking the whole prompt instead put the contact roster —
    ordered by updated_at, so reordered by essentially every save_meeting —
    inside the cached prefix. Every reorder was then a full miss on the
    system prompt *and* the tool definitions, rewritten at 1.25x, which on a
    workload where most turns file a meeting costs more than not caching.
    """
    parts = [system] if isinstance(system, str) else [p for p in system if p]
    return [
        {"type": "text", "text": parts[0], "cache_control": {"type": "ephemeral"}},
        *({"type": "text", "text": p} for p in parts[1:]),
    ]


async def run_agent(
    client,
    pool,
    user: User,
    messages: list[dict],
    system: str | Sequence[str],
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
    system_blocks = _system_blocks(system)
    # One id per turn, shared by every iteration of this loop. Nothing else on
    # the row can group them, and the distribution is worth having:
    # MAX_ITERATIONS is the cap, and a turn that reaches it returns
    # FALLBACK_TEXT rather than an answer.
    turn_id = uuid4()

    for _ in range(MAX_ITERATIONS):
        started = time.monotonic()
        response = await client.messages.create(
            model=settings.model,
            max_tokens=MAX_TOKENS,
            output_config={"effort": "low"},
            system=system_blocks,
            tools=tool_defs,
            messages=messages,
        )
        # Guarded here as well as inside record(): record() swallowing its own
        # errors does not protect this turn from one raised on the way in. A
        # turn that answered correctly must not become an apology because a
        # metrics insert failed.
        try:
            await usage_mod.record(
                pool, job="turn", user=user, model=settings.model,
                usage=response.usage, stop_reason=response.stop_reason,
                duration_ms=int((time.monotonic() - started) * 1000),
                turn_id=turn_id,
            )
        except Exception:
            log.exception("could not record usage for a turn")

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
