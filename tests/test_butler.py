import pytest_asyncio

from gaia.butler import APOLOGY_TEXT, handle_turn
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
    runs: a downstream failure must not erase the record that she wrote in."""
    from gaia.core.db import messages as messages_db
    from gaia.core.db.pool import tx
    from tests.fakes import FakeWhatsApp

    _patch_anthropic(monkeypatch, _BoomClient())

    batch = [{"id": "wamid.durable", "type": "text", "text": "book Tuesday with Marco"}]
    await handle_turn(wa_user, batch, FakeWhatsApp())

    async with tx() as conn:
        history = await messages_db.recent(conn, wa_user)
    assert [h["content"] for h in history] == ["book Tuesday with Marco"]
