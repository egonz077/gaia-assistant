"""What the product costs to run, and what it produced for the money.

Two kinds of number live here. Product counts are derived on read from the
domain tables — counting them into a metrics table would duplicate the source
of truth and drift from it the first time a row is deleted or merged. Model
counts come from model_calls, which holds the only thing the app would
otherwise discard.

Aggregates only, by construction: nothing in this module selects a contact
name, a meeting summary or a message body, because the report it feeds is
designed to be screenshotted. The one exception is `users.name`, so the
per-developer table reads as people rather than uuids.
"""

# USD per million tokens. Anthropic first-party rates.
#
# Deliberately not stored on the row. Prices change, and storing a computed
# cost means either rewriting history when they do or reporting numbers that
# quietly stop being true. Tokens are the fact; cost is a view over them, so
# an old row reprices correctly when this dict is edited.
PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}
CACHE_WRITE_MULTIPLIER = 1.25   # cache_creation_input_tokens
CACHE_READ_MULTIPLIER = 0.10    # cache_read_input_tokens

# USD per MINUTE of audio. A different unit from PRICES above, and kept in its
# own dict rather than bolted into that one: a per-token rate and a per-minute
# rate sharing a shape is a mistake waiting to be made.
AUDIO_PRICES: dict[str, float] = {
    "nova-3": 0.0043,
}


def row_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation: int = 0,
    cache_read: int = 0,
    audio_seconds: float | None = None,
) -> float | None:
    """Dollars for one call, or None if the model has no rate card.

    Branches on the unit the row is billed in. A transcription row carries
    audio_seconds and zeros in every token column, so pricing it as tokens
    would report it as free rather than as differently billed.

    None rather than a guess: a number derived from the wrong rate card is
    worse than an admitted gap, because it looks like an answer. `collect`
    names the unpriced models so a zero cannot read as "cheap".
    """
    if audio_seconds is not None:
        rate = AUDIO_PRICES.get(model)
        return None if rate is None else (float(audio_seconds) / 60.0) * rate

    price = PRICES.get(model)
    if price is None:
        return None
    return (
        input_tokens * price["input"]
        + cache_creation * price["input"] * CACHE_WRITE_MULTIPLIER
        + cache_read * price["input"] * CACHE_READ_MULTIPLIER
        + output_tokens * price["output"]
    ) / 1e6


def _cost_of(row: dict) -> float:
    return row_cost(
        row["model"], row["input_tokens"], row["output_tokens"],
        row["cache_creation_input_tokens"], row["cache_read_input_tokens"],
        audio_seconds=row.get("audio_seconds"),
    ) or 0.0


async def collect(conn, days: int = 30) -> dict:
    """Every number the report shows, as plain data.

    Returns a dict rather than a dataclass so the renderer stays a pure
    function of data, and the tests can assert on it without constructing
    anything.
    """
    window = {"days": days}

    cur = await conn.execute(
        """SELECT job, model, input_tokens, output_tokens,
                  cache_creation_input_tokens, cache_read_input_tokens,
                  audio_seconds, stop_reason, created_at::date AS day, user_id
           FROM model_calls
           WHERE created_at >= now() - make_interval(days => %(days)s)""",
        window,
    )
    calls = await cur.fetchall()

    def _group(key: str) -> list[dict]:
        out: dict = {}
        for c in calls:
            bucket = out.setdefault(c[key], {
                key: c[key], "calls": 0, "input_tokens": 0, "output_tokens": 0,
                "cache_creation": 0, "cache_read": 0, "cost": 0.0,
            })
            bucket["calls"] += 1
            bucket["input_tokens"] += c["input_tokens"]
            bucket["output_tokens"] += c["output_tokens"]
            bucket["cache_creation"] += c["cache_creation_input_tokens"]
            bucket["cache_read"] += c["cache_read_input_tokens"]
            bucket["cost"] += _cost_of(c)
        return sorted(out.values(), key=lambda r: -r["cost"])

    cache_read = sum(c["cache_read_input_tokens"] for c in calls)
    cache_write = sum(c["cache_creation_input_tokens"] for c in calls)
    plain_input = sum(c["input_tokens"] for c in calls)
    denominator = cache_read + cache_write + plain_input

    stop_reasons: dict = {}
    for c in calls:
        stop_reasons[c["stop_reason"]] = stop_reasons.get(c["stop_reason"], 0) + 1

    cur = await conn.execute("SELECT id, name FROM users")
    names = {r["id"]: r["name"] for r in await cur.fetchall()}
    by_user = _group("user_id")
    for row in by_user:
        row["name"] = names.get(row["user_id"], "(deleted user)")

    # Calls per turn. turn_id is NULL for single-call jobs, which are not
    # turns and must not be counted as one-call ones.
    cur = await conn.execute(
        """SELECT calls, count(*) AS turns
           FROM (SELECT turn_id, count(*) AS calls FROM model_calls
                 WHERE turn_id IS NOT NULL
                   AND created_at >= now() - make_interval(days => %(days)s)
                 GROUP BY turn_id) per_turn
           GROUP BY calls ORDER BY calls""",
        window,
    )
    calls_per_turn = {r["calls"]: r["turns"] for r in await cur.fetchall()}

    cur = await conn.execute(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE source = 'photo_notes') AS photo,
                  count(*) FILTER (WHERE source = 'text') AS text
           FROM meetings
           WHERE created_at >= now() - make_interval(days => %(days)s)""",
        window,
    )
    meetings = await cur.fetchone()

    # A meeting with nobody in it usually means the model could not pin a name
    # down, which is worth seeing beside the photo/text split.
    cur = await conn.execute(
        """SELECT coalesce(avg(n), 0)::float AS mean, coalesce(max(n), 0) AS max
           FROM (SELECT count(mc.contact_id) AS n
                 FROM meetings m
                 LEFT JOIN meeting_contacts mc ON mc.meeting_id = m.id
                 WHERE m.created_at >= now() - make_interval(days => %(days)s)
                 GROUP BY m.id) per_meeting""",
        window,
    )
    contacts_per_meeting = await cur.fetchone()

    cur = await conn.execute(
        """SELECT m.created_at::date AS day, u.name, count(*) AS meetings
           FROM meetings m JOIN users u ON u.id = m.user_id
           WHERE m.created_at >= now() - make_interval(days => %(days)s)
           GROUP BY 1, 2 ORDER BY 1""",
        window,
    )
    meetings_by_day_and_user = [dict(r) for r in await cur.fetchall()]

    cur = await conn.execute(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE due_at IS NOT NULL) AS with_due_date,
                  count(*) FILTER (WHERE done_at IS NOT NULL) AS done
           FROM commitments
           WHERE created_at >= now() - make_interval(days => %(days)s)""",
        window,
    )
    commitments = await cur.fetchone()

    cur = await conn.execute(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE next_action_at IS NOT NULL) AS with_next_action
           FROM leads
           WHERE created_at >= now() - make_interval(days => %(days)s)""",
        window,
    )
    leads = await cur.fetchone()

    cur = await conn.execute(
        """SELECT count(*) AS active,
                  count(*) FILTER (
                      WHERE last_digest_on = (now() AT TIME ZONE 'utc')::date
                  ) AS digested_today
           FROM users WHERE active"""
    )
    users = await cur.fetchone()

    return {
        "days": days,
        "totals": {
            "calls": len(calls),
            "input_tokens": plain_input,
            "output_tokens": sum(c["output_tokens"] for c in calls),
            "cost": sum(_cost_of(c) for c in calls),
        },
        "by_job": _group("job"),
        "by_model": _group("model"),
        "by_user": by_user,
        "by_day": sorted(_group("day"), key=lambda r: r["day"]),
        "cache_hit_rate": (cache_read / denominator) if denominator else None,
        "stop_reasons": stop_reasons,
        # Checked against whichever rate card the row's unit belongs to. A
        # check that consulted only PRICES would report nova-3 — the one audio
        # vendor we do price — as unpriced.
        "unknown_models": sorted({
            c["model"] for c in calls
            if (c["audio_seconds"] is not None and c["model"] not in AUDIO_PRICES)
            or (c["audio_seconds"] is None and c["model"] not in PRICES)
        }),
        "audio_minutes": sum(float(c["audio_seconds"] or 0) for c in calls) / 60.0,
        "calls_per_turn": calls_per_turn,
        "meetings": dict(meetings),
        "contacts_per_meeting": dict(contacts_per_meeting),
        "meetings_by_day_and_user": meetings_by_day_and_user,
        "commitments": dict(commitments),
        "leads": dict(leads),
        "users": dict(users),
    }
