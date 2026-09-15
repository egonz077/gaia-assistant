from cryptography.fernet import Fernet
import pytest

from gaia.core import crypto
from gaia.core.db import google_accounts as ga


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(crypto.settings, "google_token_key", Fernet.generate_key().decode())


async def test_round_trip(conn, ana):
    await ga.upsert(conn, ana, google_email="ana@gaiagroupdevelopment.com",
                    refresh_token="1//tok", scopes="calendar.events.owned")
    got = await ga.get(conn, ana)
    assert got["google_email"] == "ana@gaiagroupdevelopment.com"
    assert got["refresh_token"] == "1//tok"
    assert got["revoked_at"] is None
    assert got["revoked_notified_at"] is None


async def test_stored_column_is_encrypted(conn, ana):
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//tok", scopes="s")
    cur = await conn.execute("SELECT refresh_token_enc FROM google_accounts")
    assert "1//tok" not in (await cur.fetchone())["refresh_token_enc"]


async def test_one_user_cannot_see_anothers_grant(conn, ana, sofia):
    """Ownership, not visibility. There is no setting at which a colleague's
    token is readable, which is why this table has no visibility column."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//tok", scopes="s")
    assert await ga.get(conn, sofia) is None


async def test_reconnect_replaces_the_grant(conn, ana):
    """A second consent must not leave the revoked token behind, and must clear
    revoked_at -- otherwise reconnecting appears to work and then fails."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//old", scopes="s")
    await ga.revoke(conn, ana)
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//new", scopes="s")
    got = await ga.get(conn, ana)
    assert got["refresh_token"] == "1//new" and got["revoked_at"] is None


async def test_revoke_marks_rather_than_deletes(conn, ana):
    """Kept so the digest can say 'your calendar disconnected' once, rather
    than silently losing the fact that it ever existed."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//tok", scopes="s")
    await ga.revoke(conn, ana)
    assert (await ga.get(conn, ana))["revoked_at"] is not None


async def test_mark_revoked_notified_stamps_it(conn, ana):
    """A DATE (last_digest_on) can't answer "have we told them" -- only "what
    day is it". This column is a fact of its own, set at the moment the
    digest actually says the grant is gone."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//tok", scopes="s")
    await ga.revoke(conn, ana)
    assert (await ga.get(conn, ana))["revoked_notified_at"] is None

    await ga.mark_revoked_notified(conn, ana)
    assert (await ga.get(conn, ana))["revoked_notified_at"] is not None


async def test_reconnect_clears_the_notified_flag_too(conn, ana):
    """A second revocation after a reconnect is a new event and must be
    announced again -- otherwise the flag from the first revocation silences
    the digest about the second one forever."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//old", scopes="s")
    await ga.revoke(conn, ana)
    await ga.mark_revoked_notified(conn, ana)

    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//new", scopes="s")
    assert (await ga.get(conn, ana))["revoked_notified_at"] is None
