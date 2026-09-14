from datetime import datetime, timedelta, timezone

from gaia.core import stats
from gaia.core.db import meetings as meetings_db


def test_cost_of_a_known_row_including_cache_multipliers():
    """A fixed token mix produces a fixed dollar figure. Opus 5 at $5/$25 per
    million: 1000 plain input, 1000 cache writes at 1.25x, 10000 cache reads
    at 0.10x, 500 output."""
    cost = stats.row_cost("claude-opus-5", 1000, 500, 1000, 10000)

    expected = (1000 * 5 + 1000 * 5 * 1.25 + 10000 * 5 * 0.10 + 500 * 25) / 1e6
    assert cost == expected
    # Spelled out as well as derived, so a plausible-looking change to the
    # formula has to survive a number somebody worked out by hand:
    # 5000 + 6250 + 5000 + 12500 = 28750 millionths.
    assert round(cost, 6) == 0.02875


def test_an_unknown_model_reports_no_cost_rather_than_guessing():
    """Prices change and models are added. A number derived from the wrong
    rate card is worse than an admitted gap, because it looks like an answer."""
    assert stats.row_cost("claude-from-the-future", 1000, 500, 0, 0) is None


def test_every_model_the_app_can_be_configured_with_has_a_price():
    """MODEL and DIGEST_MODEL both default to something in this dict, and
    .env.example offers haiku as a documented option."""
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"):
        assert model in stats.PRICES


async def test_collect_totals_tokens_and_cost_by_job(conn, ana):
    await conn.execute(
        """INSERT INTO model_calls (job, user_id, model, input_tokens, output_tokens)
           VALUES ('turn', %(u)s, 'claude-opus-5', 1000, 100),
                  ('turn', %(u)s, 'claude-opus-5', 2000, 200),
                  ('digest', %(u)s, 'claude-sonnet-5', 500, 50)""",
        {"u": ana.id},
    )

    data = await stats.collect(conn, days=30)

    by_job = {r["job"]: r for r in data["by_job"]}
    assert by_job["turn"]["calls"] == 2
    assert by_job["turn"]["input_tokens"] == 3000
    assert by_job["digest"]["calls"] == 1
    assert by_job["turn"]["cost"] > by_job["digest"]["cost"]
    assert data["totals"]["calls"] == 3


async def test_collect_reports_the_cache_hit_rate(conn, ana):
    """cache_read / (cache_read + cache_creation + input) — the number that
    settles whether the _system_blocks breakpoint design pays off."""
    await conn.execute(
        """INSERT INTO model_calls (job, user_id, model, input_tokens, output_tokens,
                                  cache_creation_input_tokens, cache_read_input_tokens)
           VALUES ('turn', %s, 'claude-opus-5', 100, 10, 300, 600)""",
        (ana.id,),
    )

    data = await stats.collect(conn, days=30)

    assert data["cache_hit_rate"] == 0.6


async def test_collect_reports_the_calls_per_turn_distribution(conn, ana):
    """MAX_ITERATIONS is 8 and a turn that hits the cap returns FALLBACK_TEXT,
    so the tail of this distribution is the part worth seeing. Rows with a
    NULL turn_id are single-call jobs, not one-call turns."""
    await conn.execute(
        """INSERT INTO model_calls (job, user_id, model, input_tokens, output_tokens, turn_id)
           VALUES ('turn', %(u)s, 'claude-opus-5', 1, 1, '11111111-1111-1111-1111-111111111111'),
                  ('turn', %(u)s, 'claude-opus-5', 1, 1, '11111111-1111-1111-1111-111111111111'),
                  ('turn', %(u)s, 'claude-opus-5', 1, 1, '22222222-2222-2222-2222-222222222222'),
                  ('digest', %(u)s, 'claude-sonnet-5', 1, 1, NULL)""",
        {"u": ana.id},
    )

    data = await stats.collect(conn, days=30)

    assert data["calls_per_turn"] == {1: 1, 2: 1}, "the digest row is not a turn"


async def test_collect_reports_contacts_per_meeting(conn, ana):
    await meetings_db.save(
        conn, ana, summary="Two people", source="text",
        contact_names=["Marta Delgado", "Tyler Reed"],
    )
    await meetings_db.save(conn, ana, summary="Nobody named", source="text")

    data = await stats.collect(conn, days=30)

    assert data["contacts_per_meeting"]["max"] == 2
    assert data["contacts_per_meeting"]["mean"] == 1.0


async def test_collect_counts_meetings_and_their_commitments(conn, ana):
    """Product numbers are derived, never stored — counting them into a
    metrics table would duplicate the source of truth and drift from it."""
    await meetings_db.save(
        conn, ana, summary="Coral Gables", source="photo_notes",
        contact_names=["Marta Delgado"],
        commitments=[{"description": "Send comps",
                      "due_at": datetime.now(timezone.utc) + timedelta(days=2)},
                     {"description": "Call the attorney"}],
    )

    data = await stats.collect(conn, days=30)

    assert data["meetings"]["total"] == 1
    assert data["meetings"]["photo"] == 1
    assert data["commitments"]["total"] == 2
    assert data["commitments"]["with_due_date"] == 1


async def test_collect_ignores_rows_outside_the_window(conn, ana):
    await conn.execute(
        """INSERT INTO model_calls (job, user_id, model, input_tokens, output_tokens, created_at)
           VALUES ('turn', %s, 'claude-opus-5', 1000, 100, now() - interval '60 days')""",
        (ana.id,),
    )

    data = await stats.collect(conn, days=30)

    assert data["totals"]["calls"] == 0


async def test_an_unknown_model_is_named_in_the_output(conn, ana):
    """Reported, not hidden: the tokens are counted and the report says which
    model has no rate card, so a silently-zero cost cannot be mistaken for a
    cheap one."""
    await conn.execute(
        """INSERT INTO model_calls (job, user_id, model, input_tokens, output_tokens)
           VALUES ('turn', %s, 'claude-from-the-future', 1000, 100)""",
        (ana.id,),
    )

    data = await stats.collect(conn, days=30)

    assert data["unknown_models"] == ["claude-from-the-future"]
    assert data["totals"]["cost"] == 0.0
    assert data["totals"]["calls"] == 1


async def test_collect_on_an_empty_database_reports_zeros(conn):
    """A fresh deploy must produce a readable report, not a crash or a
    division by zero."""
    data = await stats.collect(conn, days=30)

    assert data["totals"]["calls"] == 0
    assert data["totals"]["cost"] == 0.0
    assert data["cache_hit_rate"] is None
    assert data["by_job"] == []
    assert data["calls_per_turn"] == {}
    assert data["contacts_per_meeting"]["max"] == 0


def test_audio_is_priced_per_minute_not_per_token():
    """Transcription bills in audio-seconds. 120 seconds of nova-3 at
    $0.0043/min is $0.0086 — and the token columns are zero, so a token-based
    reading of the same row would report it as free rather than as differently
    billed."""
    cost = stats.row_cost("nova-3", 0, 0, 0, 0, audio_seconds=120)

    assert round(cost, 6) == round(2 * 0.0043, 6)


def test_an_unpriced_audio_vendor_reports_no_cost():
    assert stats.row_cost("some-other-asr", 0, 0, 0, 0, audio_seconds=60) is None


def test_token_pricing_is_unaffected_by_the_audio_branch():
    """The existing path must not move. Same fixed mix as before."""
    assert round(stats.row_cost("claude-opus-5", 1000, 500, 1000, 10000), 6) == 0.02875


async def test_collect_reports_audio_minutes_and_transcription_cost(conn, ana):
    await conn.execute(
        """INSERT INTO model_calls (job, user_id, model, input_tokens, output_tokens,
                                    audio_seconds)
           VALUES ('transcription', %s, 'nova-3', 0, 0, 150)""",
        (ana.id,),
    )

    data = await stats.collect(conn, days=30)

    assert data["audio_minutes"] == 2.5
    by_job = {r["job"]: r for r in data["by_job"]}
    assert round(by_job["transcription"]["cost"], 6) == round(2.5 * 0.0043, 6)
    assert data["unknown_models"] == [], (
        "nova-3 is priced in AUDIO_PRICES, not PRICES — an unknown-model check "
        "that only consults PRICES would report the one vendor we do price"
    )


async def test_an_unpriced_audio_model_is_named_not_silently_free(conn, ana):
    """The token path already does this. The audio path must too, or a vendor
    with no rate card reports $0.00 and reads as cheap."""
    await conn.execute(
        """INSERT INTO model_calls (job, user_id, model, input_tokens, output_tokens,
                                    audio_seconds)
           VALUES ('transcription', %s, 'whisper-somewhere', 0, 0, 60)""",
        (ana.id,),
    )

    data = await stats.collect(conn, days=30)

    assert data["unknown_models"] == ["whisper-somewhere"]
    assert data["totals"]["cost"] == 0.0
