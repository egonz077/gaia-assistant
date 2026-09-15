from datetime import date, timedelta

from cryptography.fernet import Fernet
import httpx
import pytest

from gaia.core import crypto
from gaia.core.db import google_accounts as ga
from gaia.jobs import digest


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(crypto.settings, "google_token_key", Fernet.generate_key().decode())


def _revoking_http():
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_revocation_is_announced_on_the_first_morning_after(conn, ana):
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")
    async with _revoking_http() as http:
        out = await digest.calendar_section(
            conn, ana, http=http, today="2026-09-16",
            last_digest_on=date(2026, 9, 15),
        )
    assert out == {"revoked": True}


async def test_revocation_is_not_repeated_the_next_morning(conn, ana):
    """Someone may have revoked us deliberately. Telling them every morning
    is its own failure."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")
    await ga.revoke(conn, ana)
    async with _revoking_http() as http:
        out = await digest.calendar_section(
            conn, ana, http=http, today="2026-09-18",
            last_digest_on=date.today() + timedelta(days=1),
        )
    assert out is None


async def test_a_user_who_never_connected_is_never_nagged(conn, ana):
    async with _revoking_http() as http:
        out = await digest.calendar_section(
            conn, ana, http=http, today="2026-09-16", last_digest_on=date(2026, 9, 15))
    assert out is None
