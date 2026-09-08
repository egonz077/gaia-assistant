from datetime import datetime, timedelta, timezone

from gaia.core.db import leads as leads_db
from gaia.core.db import users as users_db
from gaia.jobs import digest
from tests.fakes import FakeAnthropic, FakeResponse, FakeWhatsApp, TextBlock


async def test_quiet_day_sends_nothing(conn, ana):
    wa = FakeWhatsApp()
    sent = await digest.send_digest(conn, FakeAnthropic([]), wa, ana)
    assert sent is False
    assert wa.sent == []


async def test_due_lead_produces_a_message(conn, ana):
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

    assert await digest.send_digest(conn, client, wa, ana) is True
    assert wa.sent[0][0] == ana.wa_id
    assert "Maria" in wa.sent[0][1]
    assert wa.templates == []


async def test_digest_excludes_a_colleagues_due_lead(conn, ana, sofia):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await leads_db.create(
        conn, sofia, contact_name="Rivera", description="Selling", next_action_at=past
    )
    wa = FakeWhatsApp()
    assert await digest.send_digest(conn, FakeAnthropic([]), wa, ana) is False


async def test_nudge_count_increments_and_reaches_the_prompt(conn, ana):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await leads_db.create(
        conn, ana, contact_name="Maria", description="Buying", next_action_at=past
    )
    wa = FakeWhatsApp()

    client = FakeAnthropic([FakeResponse([TextBlock("first")])])
    await digest.send_digest(conn, client, wa, ana)

    client2 = FakeAnthropic([FakeResponse([TextBlock("second")])])
    await digest.send_digest(conn, client2, wa, ana)

    prompt = str(client2.requests[0]["messages"])
    assert "nudge_count" in prompt and "1" in prompt


async def test_outside_the_window_a_template_is_used(conn, ana):
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

    await digest.send_digest(conn, client, wa, ana)
    assert wa.sent == [] and len(wa.templates) == 1


async def test_never_messaged_a_template_is_used(conn, ana):
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

    await digest.send_digest(conn, client, wa, ana)
    assert wa.sent == [] and len(wa.templates) == 1


async def test_a_rejected_send_records_nothing(conn, ana):
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

    assert await digest.send_digest(conn, client, wa, ana) is False

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
