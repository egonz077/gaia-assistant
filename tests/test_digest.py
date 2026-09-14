import re
from datetime import datetime, timedelta, timezone

from gaia.core.db import leads as leads_db
from gaia.core.db import users as users_db
from gaia.jobs import digest
from tests.fakes import FakeAnthropic, FakeResponse, FakeWhatsApp, TextBlock


async def test_quiet_day_sends_nothing(conn, ana, migrated):
    wa = FakeWhatsApp()
    sent = await digest.send_digest(conn, FakeAnthropic([]), wa, ana, pool=migrated)
    assert sent is False
    assert wa.sent == []


async def test_due_lead_produces_a_message(conn, ana, migrated):
    # A recent inbound message opens the 24h customer service window, so the
    # free-form send_text path is used rather than the template.
    await users_db.touch_inbound(conn, ana)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await leads_db.create(
        conn, ana, contact_name="Maria Delgado", description="Buying",
        next_action_at=past, next_action_note="Send Friday listings",
    )
    wa = FakeWhatsApp()
    client = FakeAnthropic([FakeResponse([TextBlock("Morning! Maria Delgado is due.")])])

    assert await digest.send_digest(conn, client, wa, ana, pool=migrated) is True
    assert wa.sent[0][0] == ana.wa_id
    assert "Maria" in wa.sent[0][1]
    assert wa.templates == []


async def test_digest_excludes_a_colleagues_due_lead(conn, ana, sofia, migrated):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await leads_db.create(
        conn, sofia, contact_name="Rivera", description="Selling", next_action_at=past
    )
    wa = FakeWhatsApp()
    assert await digest.send_digest(conn, FakeAnthropic([]), wa, ana, pool=migrated) is False


async def test_nudge_count_increments_and_reaches_the_prompt(conn, ana, migrated):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await leads_db.create(
        conn, ana, contact_name="Maria", description="Buying", next_action_at=past
    )
    wa = FakeWhatsApp()

    client = FakeAnthropic([FakeResponse([TextBlock("first")])])
    await digest.send_digest(conn, client, wa, ana, pool=migrated)

    client2 = FakeAnthropic([FakeResponse([TextBlock("second")])])
    await digest.send_digest(conn, client2, wa, ana, pool=migrated)

    prompt = str(client2.requests[0]["messages"])
    assert "nudge_count" in prompt and "1" in prompt


async def test_outside_the_window_a_template_is_used(conn, ana, migrated):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await leads_db.create(
        conn, ana, contact_name="Maria", description="Buying", next_action_at=past
    )
    await conn.execute(
        "UPDATE users SET last_inbound_at = now() - interval '30 hours' WHERE id = %s",
        (ana.id,),
    )
    wa = FakeWhatsApp()
    client = FakeAnthropic([FakeResponse([TextBlock("Morning!")])])

    await digest.send_digest(conn, client, wa, ana, pool=migrated)
    assert wa.sent == [] and len(wa.templates) == 1


async def test_never_messaged_a_template_is_used(conn, ana, migrated):
    """The newly-onboarded case: an admin adds the user via the CLI and she
    has not texted the number yet, so `last_inbound_at` is NULL. No inbound
    message means no open customer-service window at all — not an unknown
    one — so this must take the template path exactly like a stale window,
    not the free-form path. This is the case that regressed once already."""
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await leads_db.create(
        conn, ana, contact_name="Maria", description="Buying", next_action_at=past
    )
    wa = FakeWhatsApp()
    client = FakeAnthropic([FakeResponse([TextBlock("Morning!")])])

    await digest.send_digest(conn, client, wa, ana, pool=migrated)
    assert wa.sent == [] and len(wa.templates) == 1


async def test_a_rejected_send_records_nothing(conn, ana, migrated):
    """A rejected template, an expired token or a rate limit used to leave
    nudge counts incremented, the digest logged into her thread as though she
    had read it, and last_digest_on set so today would not be retried. She
    got nothing and the system believed it had told her."""
    from gaia.core.db import messages as messages_db

    past = datetime.now(timezone.utc) - timedelta(days=1)
    await leads_db.create(
        conn, ana, contact_name="Maria", description="Buying", next_action_at=past
    )
    wa = FakeWhatsApp(reject_sends=True)
    client = FakeAnthropic([FakeResponse([TextBlock("Morning! Maria is due.")])])

    assert await digest.send_digest(conn, client, wa, ana, pool=migrated) is False

    due = await leads_db.due_for(conn, ana)
    assert [r["nudge_count"] for r in due] == [0]
    assert await messages_db.recent(conn, ana) == []
    cur = await conn.execute("SELECT last_digest_on FROM users WHERE id = %s", (ana.id,))
    assert (await cur.fetchone())["last_digest_on"] is None


async def test_one_unusable_user_does_not_cost_everyone_else_their_digest(conn, ana, sofia):
    """`users.timezone` is plain TEXT. ZoneInfo() on a typo raised inside
    due_users' loop, which is inside run_once, whose exception is caught only
    at the top of main() — so one bad row meant *nobody* got a digest, with a
    single `digest run failed` line every fifteen minutes."""
    await conn.execute(
        "UPDATE users SET timezone = 'America/NewYork' WHERE id = %s", (ana.id,)
    )

    due = await digest.due_users(conn, datetime(2026, 9, 8, 18, tzinfo=timezone.utc))

    assert [u.id for u in due] == [sofia.id]


async def test_a_user_added_after_8am_is_not_due_until_the_next_morning(conn, ana):
    """`last_digest_on` NULL used to mean "due right now" at any hour past
    08:00, so a user added at lunchtime got a digest minutes later — built
    from leads created moments earlier, and marking them nudged, so the next
    real digest called them "still open from yesterday"."""
    await conn.execute(
        "UPDATE users SET created_at = %s WHERE id = %s",
        (datetime(2026, 9, 12, 18, 42, tzinfo=timezone.utc), ana.id),  # 14:42 ET
    )

    due = await digest.due_users(conn, datetime(2026, 9, 12, 19, 0, tzinfo=timezone.utc))

    assert due == []


async def test_that_same_user_is_due_the_following_morning(conn, ana):
    await conn.execute(
        "UPDATE users SET created_at = %s WHERE id = %s",
        (datetime(2026, 9, 12, 18, 42, tzinfo=timezone.utc), ana.id),
    )

    due = await digest.due_users(conn, datetime(2026, 9, 13, 12, 5, tzinfo=timezone.utc))

    assert [u.id for u in due] == [ana.id]


async def test_an_older_user_who_missed_8am_is_still_caught_up_later_that_day(conn, ana):
    """The retry path `send_digest` depends on: a send that failed at 08:00,
    or a jobs container down across that hour, must still go out on a later
    tick. Only rows created *today* are held back."""
    await conn.execute(
        "UPDATE users SET created_at = %s WHERE id = %s",
        (datetime(2026, 9, 10, 13, 0, tzinfo=timezone.utc), ana.id),
    )

    due = await digest.due_users(conn, datetime(2026, 9, 12, 16, 0, tzinfo=timezone.utc))

    assert [u.id for u in due] == [ana.id]


async def test_the_composer_uses_the_digest_model_not_the_butlers(conn, ana, migrated):
    """The digest is one short paragraph written from a ~300-token JSON payload:
    no tools, no images, nothing to reason about. It must not silently ride on
    whatever the agent loop is set to — that is what made it cost Opus rates for
    a task Opus was not doing anything with."""
    from gaia.core.config import settings

    await users_db.touch_inbound(conn, ana)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await leads_db.create(
        conn, ana, contact_name="Maria", description="Buying", next_action_at=past
    )
    client = FakeAnthropic([FakeResponse([TextBlock("Morning!")])])

    await digest.send_digest(conn, client, FakeWhatsApp(), ana, pool=migrated)

    assert client.requests[0]["model"] == settings.digest_model
    assert settings.digest_model != settings.model, (
        "the split is pointless if both settings hold the same value by default"
    )


async def test_the_due_time_reaches_the_prompt_and_the_prompt_asks_for_it(conn, ana, migrated):
    """A commitment due at 5pm is only actionable if the message says 5pm.

    The payload has always carried the timestamp; nothing asked the model to
    use it, and on a cheaper model that is the difference between "call the
    attorney, due by 5pm today" and "call the attorney". Opus spent its own
    judgement filling the gap in — which is a bad reason to pay Opus rates."""
    from gaia.core.db import meetings as meetings_db

    await users_db.touch_inbound(conn, ana)
    due = datetime.now(timezone.utc) + timedelta(hours=5)
    await meetings_db.save(
        conn, ana, summary="Okonkwo title", source="text",
        commitments=[{"description": "Call the closing attorney", "due_at": due}],
    )
    client = FakeAnthropic([FakeResponse([TextBlock("Morning!")])])

    await digest.send_digest(conn, client, FakeWhatsApp(), ana, pool=migrated)

    # Compared as an instant, not as rendered text: `due_at` is timestamptz and
    # comes back in the session's timezone (America/New_York on the compose db
    # service), so a substring match against a UTC isoformat disagrees with
    # itself by four hours. Same trap as test_meetings.py.
    sent = re.search(r"'due': '([^']+)'", str(client.requests[0]["messages"]))
    assert sent, "the due timestamp never reached the model"
    assert datetime.fromisoformat(sent.group(1)) == due
    assert "due" in client.requests[0]["system"].lower(), (
        "nothing in the system prompt tells the model to name the deadline"
    )


async def test_composing_a_digest_is_recorded_against_the_recipient(migrated):
    """Per-developer cost is the point of the user_id column, and the digest
    is the one job where a row maps to exactly one person.

    The user is committed in its own transaction first: record() inserts on
    another pooled connection, so a user that exists only inside this test's
    transaction fails the foreign key there and the error is swallowed,
    leaving a test that asserts nothing.
    """
    from psycopg.rows import dict_row

    from gaia.core.config import settings
    from gaia.core.db.pool import tx

    async with tx(migrated) as conn:
        user = await users_db.create_user(conn, name="Ana", wa_id="13055559002")

    async with tx(migrated) as conn:
        await users_db.touch_inbound(conn, user)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        await leads_db.create(
            conn, user, contact_name="Maria", description="Buying", next_action_at=past
        )
        client = FakeAnthropic([FakeResponse([TextBlock("Morning!")])])
        assert await digest.send_digest(
            conn, client, FakeWhatsApp(), user, pool=migrated
        ) is True

    async with migrated.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute("SELECT job, user_id, model, turn_id FROM llm_calls")
        rows = await cur.fetchall()

    assert len(rows) == 1
    assert rows[0]["job"] == "digest"
    assert rows[0]["user_id"] == user.id
    assert rows[0]["model"] == settings.digest_model
    assert rows[0]["turn_id"] is None, (
        "a digest is one call, not a turn — counting it as a one-call turn "
        "would skew the calls-per-turn distribution"
    )


async def test_a_recording_failure_does_not_cost_the_digest(conn, ana, migrated, monkeypatch):
    """The send matters more than the metric. A telemetry failure at 8am must
    not be the reason nobody gets their morning message."""
    async def boom(*a, **kw):
        raise RuntimeError("telemetry is down")

    monkeypatch.setattr("gaia.jobs.digest.usage_mod.record", boom)

    await users_db.touch_inbound(conn, ana)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await leads_db.create(
        conn, ana, contact_name="Maria", description="Buying", next_action_at=past
    )
    wa = FakeWhatsApp()
    client = FakeAnthropic([FakeResponse([TextBlock("Morning!")])])

    assert await digest.send_digest(conn, client, wa, ana, pool=migrated) is True
    assert len(wa.sent) == 1
