from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from gaia.butler import APOLOGY_TEXT, handle_turn, today_line
from tests.fakes import FakeAnthropic, FakeResponse, TextBlock


@pytest_asyncio.fixture
async def wa_user(migrated, monkeypatch):
    """A user committed on its own connection, with the app's global pool
    pointed at the test's `migrated` pool — handle_turn opens its own
    transactions via the module-level pool, a different connection than
    the `conn` fixture would give us, so the user must actually be
    committed rather than sitting in another session's open transaction."""
    from gaia.core.db import pool as pool_module
    from gaia.core.db import users as users_db
    from gaia.core.db.pool import tx

    monkeypatch.setattr(pool_module, "_pool", migrated)
    async with tx(migrated) as c:
        return await users_db.create_user(c, name="Ana", wa_id="13055550001")


def _patch_anthropic(monkeypatch, client):
    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda: client)


async def _receive(user, batch):
    """What the webhook does before a burst is queued: log each inbound
    message in the same transaction as the dedup check (butler.receive).
    Inbound logging lives there, not in handle_turn, so that a redelivery
    arriving mid-turn is recognised as a duplicate rather than run twice."""
    from gaia.butler import receive
    from gaia.core.db.pool import tx

    async with tx() as conn:
        for message in batch:
            await receive(conn, user, message)


class _BoomClient:
    """Simulates the agent loop blowing up — an Anthropic API error, a
    dropped connection, anything unhandled inside run_agent."""

    def __init__(self):
        self.messages = self

    async def create(self, **kwargs):
        raise RuntimeError("the model call blew up")


async def test_agent_loop_failure_sends_an_apology_not_silence(wa_user, monkeypatch):
    from tests.fakes import FakeWhatsApp

    wa = FakeWhatsApp()
    _patch_anthropic(monkeypatch, _BoomClient())

    batch = [{"id": "wamid.1", "type": "text", "text": "book Tuesday with Marco"}]
    await handle_turn(wa_user, batch, wa)

    assert wa.sent == [(wa_user.wa_id, APOLOGY_TEXT)]


async def test_apology_send_failing_does_not_raise(wa_user, monkeypatch):
    class _AlwaysFailsWhatsApp:
        async def send_text(self, to, body):
            raise RuntimeError("whatsapp graph api is down")

        async def download_media(self, media_id):
            raise AssertionError("no images in this batch")

    _patch_anthropic(monkeypatch, _BoomClient())

    batch = [{"id": "wamid.1", "type": "text", "text": "hello"}]
    # Must not raise even though both the agent loop AND the apology fail.
    await handle_turn(wa_user, batch, _AlwaysFailsWhatsApp())


async def test_one_bad_image_does_not_lose_the_rest_of_the_burst(wa_user, monkeypatch):
    from tests.fakes import FakeWhatsApp

    wa = FakeWhatsApp(media_errors={"bad-media"})
    client = FakeAnthropic([FakeResponse([TextBlock("Got the Tuesday note, one photo didn't come through.")])])
    _patch_anthropic(monkeypatch, client)

    batch = [
        {"id": "wamid.1", "type": "image", "image_id": "bad-media", "caption": "whiteboard"},
        {"id": "wamid.2", "type": "text", "text": "book Tuesday with Marco"},
    ]
    await handle_turn(wa_user, batch, wa)

    # The bad image was attempted, failed, and did not stop the rest of the
    # turn: the reply path was reached and a real reply — not an apology —
    # went out.
    assert wa.downloaded == ["bad-media"]
    assert wa.sent == [(wa_user.wa_id, "Got the Tuesday note, one photo didn't come through.")]

    # The model actually saw a note about the failed photo, not silence.
    [request] = client.requests
    last_message = request["messages"][-1]
    texts = [b["text"] for b in last_message["content"] if b["type"] == "text"]
    assert any("could not be downloaded" in t for t in texts)


async def test_inbound_messages_survive_an_agent_loop_failure(wa_user, monkeypatch):
    """The whole point of committing the inbound log before the agent loop
    runs: a downstream failure must not erase the record that she wrote in.

    The apology is also logged (see test_apology_is_logged_to_history) —
    what matters here specifically is that her own note is untouched by the
    later failure, not merely that *some* history exists.
    """
    from gaia.core.db import messages as messages_db
    from gaia.core.db.pool import tx
    from tests.fakes import FakeWhatsApp

    _patch_anthropic(monkeypatch, _BoomClient())

    batch = [{"id": "wamid.durable", "type": "text", "text": "book Tuesday with Marco"}]
    await _receive(wa_user, batch)
    await handle_turn(wa_user, batch, FakeWhatsApp())

    async with tx() as conn:
        history = await messages_db.recent(conn, wa_user)
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "book Tuesday with Marco"


async def test_failed_image_download_still_surfaces_the_caption(wa_user, monkeypatch):
    """The caption is often the actual content — "offer at 580, wants to
    close by Nov" — and is the one thing of hers we still have when the
    photo itself can't be downloaded. It must reach the model, not just the
    generic failure note."""
    from tests.fakes import FakeWhatsApp

    wa = FakeWhatsApp(media_errors={"bad-media"})
    client = FakeAnthropic([
        FakeResponse([TextBlock("Got the Delgado note, the photo didn't come through though.")])
    ])
    _patch_anthropic(monkeypatch, client)

    caption = "Delgado showing, offer at 580, wants to close by Nov"
    batch = [{"id": "wamid.cap", "type": "image", "image_id": "bad-media", "caption": caption}]
    await _receive(wa_user, batch)
    await handle_turn(wa_user, batch, wa)

    [request] = client.requests
    last_message = request["messages"][-1]
    texts = [b["text"] for b in last_message["content"] if b["type"] == "text"]
    assert caption in texts
    assert any("could not be downloaded" in t for t in texts)

    # And her own history says so too. The row was written at the webhook
    # before the photo was ever fetched, so the failure has to amend it — a
    # second insert would hit ON CONFLICT DO NOTHING and the note would
    # vanish, leaving history claiming the photo arrived fine.
    from gaia.core.db import messages as messages_db
    from gaia.core.db.pool import tx

    async with tx() as conn:
        history = await messages_db.recent(conn, wa_user)
    assert caption in history[0]["content"]
    assert "could not be downloaded" in history[0]["content"]


async def test_apology_is_logged_to_history(wa_user, monkeypatch):
    """Without this row, the model sees two of her messages back to back
    after she resends, with no sign anything went wrong."""
    from gaia.core.db import messages as messages_db
    from gaia.core.db.pool import tx
    from tests.fakes import FakeWhatsApp

    _patch_anthropic(monkeypatch, _BoomClient())

    batch = [{"id": "wamid.logged", "type": "text", "text": "book Tuesday"}]
    await _receive(wa_user, batch)
    await handle_turn(wa_user, batch, FakeWhatsApp())

    async with tx() as conn:
        history = await messages_db.recent(conn, wa_user)
    assert [(h["role"], h["content"]) for h in history] == [
        ("user", "book Tuesday"),
        ("assistant", APOLOGY_TEXT),
    ]


async def test_apology_logging_failure_does_not_raise(wa_user, monkeypatch):
    """Recording the apology is strictly secondary to sending it — a DB
    error while logging it must not surface as a second turn failure."""
    from gaia.core.db import messages as messages_db
    from tests.fakes import FakeWhatsApp

    original_log = messages_db.log

    async def flaky_log(conn, user, role, content, wa_msg_id=None):
        if role == "assistant" and content == APOLOGY_TEXT:
            raise RuntimeError("db write failed while logging the apology")
        return await original_log(conn, user, role, content, wa_msg_id)

    monkeypatch.setattr(messages_db, "log", flaky_log)
    _patch_anthropic(monkeypatch, _BoomClient())

    wa = FakeWhatsApp()
    batch = [{"id": "wamid.flaky", "type": "text", "text": "hello again"}]
    # Must not raise even though logging the apology itself fails.
    await handle_turn(wa_user, batch, wa)

    assert wa.sent == [(wa_user.wa_id, APOLOGY_TEXT)]


class TestSystemPrompt:
    """The assembled prompt, read as the user would read it.

    Spec §9.2 asks that the request carry a roster rather than full profiles;
    §5.3 asks for the cache breakpoint after the stable prefix. Both are
    properties of what build_system_prompt returns, and neither had a test.
    """

    async def test_stable_first_volatile_second(self, conn, ana):
        from gaia.butler import build_system_prompt

        stable, volatile = await build_system_prompt(conn, ana)

        assert "You are the assistant for Ana" in stable
        assert "Today is" not in stable
        assert "worked with recently" not in stable
        assert "Today is" in volatile
        assert ana.timezone in volatile

    async def test_carries_a_roster_of_names_not_profiles(self, conn, ana):
        from gaia.butler import build_system_prompt
        from gaia.core.db import contacts as contacts_db
        from gaia.core.db import meetings as meetings_db

        await meetings_db.save(
            conn, ana, summary="Showing", source="text", contact_names=("Maria Delgado",)
        )
        cid = await contacts_db.get_or_create(conn, ana, "Maria Delgado")
        await contacts_db.merge_profile(conn, ana, cid, "budget tops out at 600k")

        _, volatile = await build_system_prompt(conn, ana)

        assert "Maria Delgado" in volatile
        assert "600k" not in volatile

    async def test_says_nothing_about_the_users_gender(self, conn, ana):
        """Gaia's agents are of any gender and the model is handed the real
        name. A prompt that also insists on "she" misgenders people in the
        first sentence of the reply."""
        from gaia.butler import build_system_prompt

        assembled = " ".join(await build_system_prompt(conn, ana)).lower()
        for word in (" she ", " her ", " hers ", " herself "):
            assert word not in assembled, f"prompt still says{word.rstrip()}"

    async def test_the_roster_line_is_true(self, conn, ana, sofia):
        """The line claims these are people the user has worked with. It is
        the one place the ownership-vs-visibility rule is stated in prose
        rather than SQL, and it was stated backwards."""
        from gaia.butler import build_system_prompt
        from gaia.core.db import meetings as meetings_db

        await meetings_db.save(
            conn, sofia, summary="Sofia's appointment", source="text",
            contact_names=("Rivera",),
        )

        _, volatile = await build_system_prompt(conn, ana)

        assert "Rivera" not in volatile
        assert "(nobody yet)" in volatile

    async def test_context_names_the_weekday_not_just_the_date(self, conn, ana):
        """The model derived the day of the week itself and got it wrong — one
        live turn called 2026-09-11 "Friday", the next called it "Thu". For a
        scheduling assistant that is the difference between comps sent on the
        right day and the wrong one.

        Cross-checked with strftime rather than with butler.WEEKDAYS, so the
        tuple is not asserting itself. Production deliberately avoids
        strftime: %A is locale-dependent, and the container's LANG must not be
        able to change what day the assistant thinks it is.
        """
        from gaia.butler import build_system_prompt

        _, volatile = await build_system_prompt(conn, ana)

        today = datetime.now(ZoneInfo(ana.timezone))
        assert f"Today is {today.strftime('%A')} {today.date().isoformat()} in" in volatile

    async def test_todays_weekday_stays_out_of_the_cached_prefix(self, conn, ana):
        """It changes daily by definition, so the live value belongs after the
        breakpoint — in the stable half it would invalidate the system prompt
        and every tool definition once a day for no benefit. (The prompt's
        fixed "Friday, Sept 11" example is a constant and caches fine, which
        is why this asserts on the rendered value rather than on the word.)
        """
        from gaia.butler import build_system_prompt

        stable, volatile = await build_system_prompt(conn, ana)

        rendered = today_line(datetime.now(ZoneInfo(ana.timezone)))
        assert rendered not in stable
        assert rendered in volatile


@pytest.mark.parametrize(
    "iso,weekday",
    [
        ("2026-09-11", "Friday"),    # the date the live smoke run got wrong
        ("2026-09-08", "Tuesday"),
        ("2026-09-13", "Sunday"),
        ("2026-01-01", "Thursday"),
        ("2026-12-31", "Thursday"),
    ],
)
def test_today_line_names_the_right_weekday(iso, weekday):
    """Real calendar facts, so an off-by-one in WEEKDAYS cannot pass."""
    moment = datetime.fromisoformat(f"{iso}T09:00:00+00:00")
    assert today_line(moment) == f"{weekday} {iso}"


def test_today_line_follows_the_users_timezone_across_midnight():
    """Derived from the same timezone-aware datetime as the date, so the two
    can never disagree — including for the agent whose local day has already
    turned over."""
    instant = datetime.fromisoformat("2026-09-12T01:30:00+00:00")

    assert today_line(instant.astimezone(ZoneInfo("America/New_York"))) == "Friday 2026-09-11"
    assert today_line(instant.astimezone(ZoneInfo("UTC"))) == "Saturday 2026-09-12"


async def test_turn_marks_the_last_message_of_a_burst_read_before_replying(wa_user, monkeypatch):
    """The receipt means "we have started on this", so it goes out when the
    turn begins — not at the webhook, and not after the reply it is meant to
    cover the wait for. One call is enough for the whole burst: marking a
    message read marks the earlier ones in that conversation too."""
    from tests.fakes import FakeWhatsApp

    wa = FakeWhatsApp()
    client = FakeAnthropic([FakeResponse([TextBlock("On it.")])])
    _patch_anthropic(monkeypatch, client)

    batch = [
        {"id": "wamid.1", "type": "text", "text": "notes from the Rivera walkthrough"},
        {"id": "wamid.2", "type": "text", "text": "oh and book Tuesday"},
    ]
    await _receive(wa_user, batch)
    await handle_turn(wa_user, batch, wa)

    assert wa.marked_read == ["wamid.2"]
    assert [name for name, _ in wa.calls] == ["mark_read", "send_text"]


async def test_a_failing_typing_indicator_does_not_cost_the_reply(wa_user, monkeypatch):
    """Cosmetic. A Graph API blip on the read receipt must not degrade into
    the apology path and lose the turn."""
    from tests.fakes import FakeWhatsApp

    class _ReadFails(FakeWhatsApp):
        async def mark_read(self, message_id: str) -> bool:
            raise RuntimeError("graph api blip")

    wa = _ReadFails()
    client = FakeAnthropic([FakeResponse([TextBlock("Got it.")])])
    _patch_anthropic(monkeypatch, client)

    batch = [{"id": "wamid.1", "type": "text", "text": "hello"}]
    await _receive(wa_user, batch)
    await handle_turn(wa_user, batch, wa)

    assert wa.sent == [(wa_user.wa_id, "Got it.")]


async def test_a_voice_note_becomes_a_labelled_text_block(conn, ana, monkeypatch):
    """Labelled, not bare. The model must know the text is a machine
    transcription so it can check names against the roster, and the message log
    must not imply she typed it."""
    from gaia import butler
    from tests.fakes import FakeWhatsApp

    async def fake_transcribe(audio, mime_type, keyterms):
        return "Met Marta Delgado at the Coral Gables listing.", 12.5

    monkeypatch.setattr("gaia.butler.transcription.transcribe", fake_transcribe)
    batch = [{"id": "wamid.1", "type": "audio", "audio_id": "m1",
              "mime_type": "audio/ogg", "voice": True}]

    blocks, wa_ids = await butler._build_blocks(conn, ana, batch, FakeWhatsApp(), None)

    assert blocks == [{
        "type": "text",
        "text": "[voice note] Met Marta Delgado at the Coral Gables listing.",
    }]
    assert wa_ids == ["wamid.1"]


async def test_the_roster_is_sent_as_keyterms(conn, ana, monkeypatch):
    """The whole trick. No ASR model has seen 'Okonkwo'; the roster is what
    makes it survive."""
    from gaia import butler
    from gaia.core.db import meetings as meetings_db
    from tests.fakes import FakeWhatsApp

    seen = {}

    async def fake_transcribe(audio, mime_type, keyterms):
        seen["keyterms"] = keyterms
        return "text", 1.0

    await meetings_db.save(conn, ana, summary="s", source="text",
                           contact_names=["Marta Delgado", "Okonkwo"])
    monkeypatch.setattr("gaia.butler.transcription.transcribe", fake_transcribe)
    batch = [{"id": "w1", "type": "audio", "audio_id": "m1",
              "mime_type": "audio/ogg", "voice": True}]

    await butler._build_blocks(conn, ana, batch, FakeWhatsApp(), None)

    assert "Okonkwo" in seen["keyterms"]
    assert "Marta Delgado" in seen["keyterms"]


async def test_a_failed_transcription_does_not_lose_the_rest_of_the_burst(
    conn, ana, monkeypatch
):
    """Same rule as an unreadable photo: one bad item becomes a visible note,
    never a dropped batch. Dictating four notes and losing all of them because
    the third had a network blip is the failure this prevents."""
    from gaia import butler
    from gaia.core.transcription import TranscriptionError
    from tests.fakes import FakeWhatsApp

    async def boom(audio, mime_type, keyterms):
        raise TranscriptionError("vendor down")

    monkeypatch.setattr("gaia.butler.transcription.transcribe", boom)
    batch = [
        {"id": "w1", "type": "audio", "audio_id": "m1",
         "mime_type": "audio/ogg", "voice": True},
        {"id": "w2", "type": "text", "text": "and the comps are due Friday"},
    ]

    blocks, wa_ids = await butler._build_blocks(conn, ana, batch, FakeWhatsApp(), None)

    texts = [b["text"] for b in blocks]
    assert butler.VOICE_FAILED_NOTE in texts
    assert "and the comps are due Friday" in texts
    assert wa_ids == ["w1", "w2"]


async def test_a_failed_download_is_also_survivable(conn, ana):
    """The vendor is not the only thing that can fail."""
    from gaia import butler
    from tests.fakes import FakeWhatsApp

    batch = [{"id": "w1", "type": "audio", "audio_id": "m1",
              "mime_type": "audio/ogg", "voice": True}]

    blocks, _ = await butler._build_blocks(
        conn, ana, batch, FakeWhatsApp(media_errors={"m1"}), None
    )

    assert blocks == [{"type": "text", "text": butler.VOICE_FAILED_NOTE}]


def test_a_voice_note_reads_as_one_in_history():
    from gaia import butler

    assert butler.transcript_text(
        {"type": "audio", "transcript": "Met Marta."}
    ) == "[voice note] Met Marta."


def test_the_prompt_warns_that_transcribed_names_may_be_wrong():
    """The echo-back is the correction point, and it only works if the model
    knows the names might be misheard."""
    from gaia.butler import BASE_PROMPT

    assert "[voice note]" in BASE_PROMPT
    assert "mishear" in BASE_PROMPT.lower()


async def test_a_transcription_is_recorded_with_its_duration(migrated, monkeypatch):
    """Priced per minute, so the duration is the billable fact. turn_id stays
    NULL: this runs before run_agent exists, and calls_per_turn counts
    agent-loop iterations — a transcription counted as one would corrupt it."""
    from psycopg.rows import dict_row

    from gaia import butler
    from gaia.core.db import users as users_db
    from gaia.core.db.pool import tx
    from tests.fakes import FakeWhatsApp

    async def fake_transcribe(audio, mime_type, keyterms):
        return "Met Marta.", 150.0

    monkeypatch.setattr("gaia.butler.transcription.transcribe", fake_transcribe)

    async with tx(migrated) as conn:
        user = await users_db.create_user(conn, name="Ana", wa_id="13055559003")
    async with tx(migrated) as conn:
        batch = [{"id": "w1", "type": "audio", "audio_id": "m1",
                  "mime_type": "audio/ogg", "voice": True}]
        await butler._build_blocks(conn, user, batch, FakeWhatsApp(), migrated)

    async with migrated.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute("SELECT * FROM model_calls")
        rows = await cur.fetchall()

    assert len(rows) == 1
    assert rows[0]["job"] == "transcription"
    assert rows[0]["model"] == "nova-3"
    assert float(rows[0]["audio_seconds"]) == 150.0
    assert rows[0]["turn_id"] is None
    assert rows[0]["user_id"] == user.id


async def test_a_failed_transcription_is_recorded_as_an_error(migrated, monkeypatch):
    """A failure rate is a product signal. Without a row it stays invisible
    until people complain that voice notes 'sometimes do nothing'."""
    from psycopg.rows import dict_row

    from gaia import butler
    from gaia.core.db import users as users_db
    from gaia.core.db.pool import tx
    from gaia.core.transcription import TranscriptionError
    from tests.fakes import FakeWhatsApp

    async def boom(audio, mime_type, keyterms):
        raise TranscriptionError("vendor down")

    monkeypatch.setattr("gaia.butler.transcription.transcribe", boom)

    async with tx(migrated) as conn:
        user = await users_db.create_user(conn, name="Ana", wa_id="13055559004")
    async with tx(migrated) as conn:
        batch = [{"id": "w1", "type": "audio", "audio_id": "m1",
                  "mime_type": "audio/ogg", "voice": True}]
        await butler._build_blocks(conn, user, batch, FakeWhatsApp(), migrated)

    async with migrated.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute("SELECT * FROM model_calls")
        rows = await cur.fetchall()

    assert len(rows) == 1
    assert rows[0]["stop_reason"] == "error"
    assert rows[0]["job"] == "transcription"


async def test_a_telemetry_failure_does_not_cost_the_transcript(conn, ana, monkeypatch):
    """The note matters more than the metric."""
    from gaia import butler
    from tests.fakes import FakeWhatsApp

    async def fake_transcribe(audio, mime_type, keyterms):
        return "Met Marta.", 10.0

    async def boom(*a, **kw):
        raise RuntimeError("telemetry is down")

    monkeypatch.setattr("gaia.butler.transcription.transcribe", fake_transcribe)
    monkeypatch.setattr("gaia.butler.usage_mod.record", boom)
    batch = [{"id": "w1", "type": "audio", "audio_id": "m1",
              "mime_type": "audio/ogg", "voice": True}]

    blocks, _ = await butler._build_blocks(conn, ana, batch, FakeWhatsApp(), None)

    assert blocks == [{"type": "text", "text": "[voice note] Met Marta."}]
