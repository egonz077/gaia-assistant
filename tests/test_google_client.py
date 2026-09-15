from cryptography.fernet import Fernet
import httpx
import pytest

from gaia.core import crypto, google
from gaia.core.db import google_accounts as ga


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(crypto.settings, "google_token_key", Fernet.generate_key().decode())


def _http(handler):
    """An httpx client wired to a handler instead of the network. The client is
    a required parameter throughout -- a seam, not a switch."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_refreshes_and_returns_an_access_token(conn, ana):
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")

    def handler(request):
        assert "oauth2.googleapis.com" in str(request.url)
        return httpx.Response(200, json={"access_token": "ya29.live"})

    async with _http(handler) as http:
        assert await google.access_token(conn, ana, http=http) == "ya29.live"


async def test_invalid_grant_revokes_and_raises(conn, ana):
    """A refresh that comes back invalid_grant means the developer revoked us
    in their Google account. Retrying forever is how an integration becomes
    invisible noise in the logs."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")

    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with _http(handler) as http:
        with pytest.raises(google.RevokedGrant):
            await google.access_token(conn, ana, http=http)
    assert (await ga.get(conn, ana))["revoked_at"] is not None


async def test_no_account_raises_revoked(conn, ana):
    async with _http(lambda r: httpx.Response(200, json={})) as http:
        with pytest.raises(google.RevokedGrant):
            await google.access_token(conn, ana, http=http)


async def test_request_attaches_the_bearer_token(conn, ana):
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")

    def handler(request):
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "ya29.live"})
        assert request.headers["authorization"] == "Bearer ya29.live"
        return httpx.Response(200, json={"ok": True})

    async with _http(handler) as http:
        assert await google.request(
            conn, ana, "GET", "https://www.googleapis.com/calendar/v3/x", http=http
        ) == {"ok": True}


async def test_html_error_response_raises_runtime_error(conn, ana):
    """A 502 with HTML body should raise RuntimeError, not JSONDecodeError,
    and should NOT revoke the grant. This happens during actual outages."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")

    def handler(request):
        return httpx.Response(502, content=b"<html><body>Bad Gateway</body></html>",
                             headers={"content-type": "text/html"})

    async with _http(handler) as http:
        with pytest.raises(RuntimeError):
            await google.access_token(conn, ana, http=http)
    # Verify grant was NOT revoked
    assert (await ga.get(conn, ana))["revoked_at"] is None


async def test_already_revoked_account_raises_revoked(conn, ana):
    """An account that exists but is already revoked should raise RevokedGrant
    without making any network calls."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")
    await ga.revoke(conn, ana)

    # Should raise RevokedGrant immediately without calling the handler
    async with _http(lambda r: httpx.Response(500, json={})) as http:
        with pytest.raises(google.RevokedGrant):
            await google.access_token(conn, ana, http=http)


async def test_non_2xx_raises_a_typed_error_carrying_the_status(conn, ana):
    """Callers need to tell 404 from everything else -- a calendar_event_id
    pointing at a deleted event is a normal end state, not a failure -- and
    parsing a status code out of an f-string is not an interface. Still a
    RuntimeError, so nothing that catches the old type breaks."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")

    def handler(request):
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "ya29"})
        return httpx.Response(404, json={"error": {"message": "Not Found"}})

    async with _http(handler) as http:
        with pytest.raises(google.GoogleAPIError) as exc:
            await google.request(conn, ana, "GET", "https://www.googleapis.com/x", http=http)
    assert exc.value.status == 404
    assert isinstance(exc.value, RuntimeError)
