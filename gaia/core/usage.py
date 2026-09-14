"""Token accounting. Counts only — this module never sees prompt or
completion text, and the table it writes to has no column to put it in."""

import logging

from gaia.core.db.pool import tx
from gaia.core.models import User

log = logging.getLogger("gaia.usage")


async def record(
    pool,
    *,
    job: str,
    user: User | None,
    model: str,
    usage,
    stop_reason: str | None,
    duration_ms: int | None,
    turn_id=None,
) -> None:
    """Write one row for one model call. Never raises.

    Telemetry must never cost a reply. `butler.py` already applies this rule
    to the typing indicator, for the same reason: a metrics insert failing
    part-way through a turn must degrade to a missing row, not to an apology
    for a turn that otherwise worked. Every exception is logged and
    swallowed — including a malformed usage object, not only a database
    failure, since what the SDK hands us is not this module's to guarantee.

    Awaited rather than fired-and-forgotten: it is one small insert against a
    pooled connection, next to a model call that just took seconds. A
    background task would buy nothing and make tests non-deterministic.

    `pool` is a required parameter passed by every caller, not an optional
    one defaulting to off. A default whose only user is a test is
    test-awareness wearing a parameter's clothes, and a switch that disables
    telemetry in production has a failure mode — the silent absence of the
    data this exists to collect — that nobody would notice for weeks.
    """
    try:
        async with tx(pool) as conn:
            await conn.execute(
                """INSERT INTO llm_calls
                       (job, user_id, model, input_tokens, output_tokens,
                        cache_creation_input_tokens, cache_read_input_tokens,
                        stop_reason, duration_ms, turn_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    job,
                    user.id if user else None,
                    model,
                    usage.input_tokens,
                    usage.output_tokens,
                    getattr(usage, "cache_creation_input_tokens", 0) or 0,
                    getattr(usage, "cache_read_input_tokens", 0) or 0,
                    stop_reason,
                    duration_ms,
                    turn_id,
                ),
            )
    except Exception:
        log.exception("could not record usage for job %s", job)
