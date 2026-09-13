"""The morning digest with a real model composing it and WhatsApp faked.

This is the one place compose_digest's request actually reaches Anthropic.
FakeAnthropic accepts every keyword argument without validating any of them, so
the `output_config={"effort": "low"}` that digest.py sends is unverified by the
entire hermetic suite — if the real API rejects that shape, nobody learns until
8am, and the failure is per-user and silent by design (run_once logs and moves
on so one bad user cannot cost the company its digests).

wa is always FakeWhatsApp. Nothing here can send a WhatsApp message; the
conftest makes constructing a real client raise.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from gaia.core.db.pool import tx
from gaia.jobs.digest import SEND_HOUR, due_users, run_once
from tests.fakes import FakeWhatsApp

pytestmark = pytest.mark.live

CONTACT = "Marta Delgado"

# Spans offsets from -11 to +14, so at any real instant at least one of these
# has a local hour inside the window below.
CANDIDATE_ZONES = (
    "Pacific/Kiritimati", "Pacific/Auckland", "Australia/Sydney", "Asia/Tokyo",
    "Asia/Shanghai", "Asia/Kolkata", "Europe/Moscow", "Europe/Madrid",
    "Europe/London", "UTC", "America/Sao_Paulo", "America/New_York",
    "America/Chicago", "America/Denver", "America/Los_Angeles",
    "Pacific/Honolulu", "Pacific/Midway",
)


def _zone_past_send_hour() -> str:
    """A timezone where it is currently past 08:00 local.

    run_once reads the real clock — only due_users takes an injectable one — so
    a fixed timezone would make these tests pass or skip depending on the hour
    the suite happens to run at. Choosing the zone from the clock instead makes
    them deterministic at every hour of the day.
    """
    now = datetime.now(timezone.utc)
    for zone in CANDIDATE_ZONES:
        if SEND_HOUR <= now.astimezone(ZoneInfo(zone)).hour <= 22:
            return zone
    raise AssertionError(
        f"no candidate zone is between {SEND_HOUR}:00 and 22:00 — widen CANDIDATE_ZONES"
    )


async def _seed_agent(pool, *, name, wa_id, zone, inbound_hours_ago):
    """An agent on an ordinary morning: on the roster since before today, with
    one overdue lead and one open commitment.

    inbound_hours_ago=None leaves last_inbound_at NULL, which _within_window
    treats as outside the 24-hour service window — the template path.

    `created_at` is backdated, and that is load-bearing rather than tidiness.
    due_users holds back a row created earlier the same day — their 08:00 has
    not come round yet — so a user created by this helper a millisecond ago is
    never selected, and every test in this file silently asserted against an
    empty digest run. An agent whose first morning has already passed is also
    simply what these tests mean by "an agent": the first-day case has its own
    coverage in tests/test_digest.py.

    Two days rather than one so the backdate cannot land on today's local date
    in any of CANDIDATE_ZONES, which span 25 hours of offsets.
    """
    from gaia.core.db import users as users_db

    async with tx(pool) as conn:
        user = await users_db.create_user(conn, name=name, wa_id=wa_id, timezone=zone)
        await conn.execute(
            "UPDATE users SET created_at = now() - interval '2 days' WHERE id = %s",
            (user.id,),
        )

        cur = await conn.execute(
            """INSERT INTO contacts (user_id, visibility, name)
               VALUES (%s, 'org', %s) RETURNING id""",
            (user.id, CONTACT),
        )
        contact_id = (await cur.fetchone())["id"]

        await conn.execute(
            """INSERT INTO leads (user_id, visibility, contact_id, description,
                                  status, next_action_at, next_action_note)
               VALUES (%s, 'org', %s, %s, 'active', now() - interval '1 day', %s)""",
            (user.id, contact_id, "Buying, around 600k",
             "Send the comparable sales she asked for"),
        )
        await conn.execute(
            """INSERT INTO commitments (user_id, visibility, contact_id, description, due_at)
               VALUES (%s, 'org', %s, %s, now() - interval '2 hours')""",
            (user.id, contact_id, "Call the listing agent back"),
        )

        if inbound_hours_ago is not None:
            await conn.execute(
                "UPDATE users SET last_inbound_at = now() - make_interval(hours => %s) WHERE id = %s",
                (inbound_hours_ago, user.id),
            )

    return user


async def test_a_real_digest_is_composed_and_delivered_free_form(claude, migrated):
    """The whole job, end to end, with the real model writing the message.

    Asserts on structure rather than phrasing — that something was sent, on the
    free-form path, naming the contact the payload contained. The SYSTEM prompt
    asks for varied wording by design, so any exact-text assertion here would
    be a flake.
    """
    user = await _seed_agent(
        migrated, name="Ana", wa_id="13055550001",
        zone=_zone_past_send_hour(), inbound_hours_ago=2,
    )
    wa = FakeWhatsApp()

    sent = await run_once(migrated, claude, wa)

    assert sent == 1, f"expected one digest, run_once reported {sent}"
    assert len(wa.sent) == 1, f"expected one free-form send, got {wa.sent}"
    assert not wa.templates, (
        f"used the template path for a user inside the 24h window: {wa.templates}"
    )

    to, body = wa.sent[0]
    assert to == user.wa_id
    assert body.strip(), "the model composed an empty digest"
    assert "Delgado" in body or "Marta" in body, (
        f"the digest never names the contact it is about: {body!r}"
    )


async def test_a_user_outside_the_service_window_gets_the_template(claude, migrated):
    """last_inbound_at NULL — a newly onboarded agent who has never texted in.

    digest.py calls this out as the highest-stakes send: an agent's very first
    digest takes the template path, and free-form sends are rejected outside
    the window. Which branch runs is decided in SQL, so it is worth checking
    against a real composed body rather than a scripted one.
    """
    await _seed_agent(
        migrated, name="Sofia", wa_id="13055550002",
        zone=_zone_past_send_hour(), inbound_hours_ago=None,
    )
    wa = FakeWhatsApp()

    sent = await run_once(migrated, claude, wa)

    assert sent == 1, f"expected one digest, run_once reported {sent}"
    assert len(wa.templates) == 1, f"expected the template path, got {wa.templates}"
    assert not wa.sent, f"free-form send used outside the service window: {wa.sent}"


async def test_a_second_run_the_same_day_sends_nothing(claude, migrated):
    """last_digest_on is what stops a restart double-sending, and the digest
    container restarts on every deploy. Checked here against the real path that
    writes it, because run_once only records the send when delivery succeeded.
    """
    await _seed_agent(
        migrated, name="Ana", wa_id="13055550001",
        zone=_zone_past_send_hour(), inbound_hours_ago=2,
    )
    wa = FakeWhatsApp()

    assert await run_once(migrated, claude, wa) == 1
    second = await run_once(migrated, claude, wa)

    assert second == 0, "the same digest was composed and sent twice in one day"
    assert len(wa.sent) == 1, f"a second message went out: {wa.sent}"


async def test_a_rejected_send_leaves_the_day_retriable(claude, migrated):
    """FakeWhatsApp(reject_sends=True) is a closed window or a dead token.

    digest.py records nothing when delivery fails, precisely so the next
    15-minute tick tries again — the alternative wrote the digest into her
    history, bumped nudge counts and set last_digest_on for a message she never
    received. The model call is real here, so this also confirms a rejected
    send is not mistaken for a compose failure.
    """
    user = await _seed_agent(
        migrated, name="Ana", wa_id="13055550001",
        zone=_zone_past_send_hour(), inbound_hours_ago=2,
    )

    sent = await run_once(migrated, claude, FakeWhatsApp(reject_sends=True))
    assert sent == 0, "a rejected send was counted as delivered"

    async with tx(migrated) as conn:
        cur = await conn.execute(
            "SELECT last_digest_on FROM users WHERE id = %s", (user.id,)
        )
        assert (await cur.fetchone())["last_digest_on"] is None, (
            "last_digest_on was set for a digest that never arrived, so today "
            "will never be retried"
        )
        cur = await conn.execute(
            "SELECT nudge_count FROM leads WHERE user_id = %s", (user.id,)
        )
        assert (await cur.fetchone())["nudge_count"] == 0, (
            "the lead was marked nudged for a message that was never delivered"
        )

    wa = FakeWhatsApp()
    assert await run_once(migrated, claude, wa) == 1, (
        "the retry after a rejected send did not go out"
    )
    assert len(wa.sent) == 1


async def test_selection_is_timezone_aware_at_a_fixed_instant(claude, migrated):
    """due_users with an injected clock — no model call, no network.

    Lives in the live tier because it is the companion assertion to the
    end-to-end tests above: they pin a zone chosen from the real clock, which
    proves delivery but deliberately says nothing about who is excluded. One
    instant, two agents, 08:00 crossed in exactly one of their timezones.
    """
    # 08:30 in New York, expressed as the UTC instant it actually is today.
    # Hardcoding 12:00 UTC would be 08:00 in New York only under EDT — from
    # November it is 07:00 there, and this test would quietly stop selecting
    # anyone for four months of the year. Los Angeles keeps the same DST rules
    # three hours behind, so 05:30 local is guaranteed on either side of the
    # switch.
    instant = (
        datetime.now(ZoneInfo("America/New_York"))
        .replace(hour=8, minute=30, second=0, microsecond=0)
        .astimezone(timezone.utc)
    )

    early = await _seed_agent(
        migrated, name="Ana", wa_id="13055550001",
        zone="America/New_York", inbound_hours_ago=2,
    )
    late = await _seed_agent(
        migrated, name="Sofia", wa_id="13055550002",
        zone="America/Los_Angeles", inbound_hours_ago=2,
    )

    async with tx(migrated) as conn:
        due = {u.id for u in await due_users(conn, now_utc=instant)}

    assert early.id in due, "08:00 in New York did not select the New York agent"
    assert late.id not in due, "05:00 in Los Angeles selected the Los Angeles agent"


async def test_an_unparseable_timezone_does_not_cost_everyone_else_their_digest(
    claude, migrated
):
    """The guard at digest.py:44. A bad users.timezone once meant nobody in the
    company got a digest, ever, with one log line every fifteen minutes.

    The CLI validates timezones now, so the only way in is a row written some
    other way — which is exactly what this does.
    """
    zone = _zone_past_send_hour()
    good = await _seed_agent(
        migrated, name="Ana", wa_id="13055550001", zone=zone, inbound_hours_ago=2,
    )
    broken = await _seed_agent(
        migrated, name="Sofia", wa_id="13055550002", zone=zone, inbound_hours_ago=2,
    )
    async with tx(migrated) as conn:
        await conn.execute(
            "UPDATE users SET timezone = %s WHERE id = %s",
            ("Amercia/New_York", broken.id),
        )

    wa = FakeWhatsApp()
    sent = await run_once(migrated, claude, wa)

    assert sent == 1, f"the typo'd row cost the healthy agent her digest (sent={sent})"
    assert wa.sent[0][0] == good.wa_id
