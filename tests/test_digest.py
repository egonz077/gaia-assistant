from datetime import datetime, timedelta, timezone

from gaia.core.db import leads as leads_db
from gaia.jobs import digest
from tests.fakes import FakeAnthropic, FakeResponse, FakeWhatsApp, TextBlock


async def test_quiet_day_sends_nothing(conn, ana):
    wa = FakeWhatsApp()
    sent = await digest.send_digest(conn, FakeAnthropic([]), wa, ana)
    assert sent is False
    assert wa.sent == []


async def test_due_lead_produces_a_message(conn, ana):
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
