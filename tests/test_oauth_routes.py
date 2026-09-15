import time

from cryptography.fernet import Fernet
import pytest
from fastapi.testclient import TestClient

from gaia import main
from gaia.core import crypto, oauth_link
from gaia.core.db import google_accounts as ga
from gaia.core.db import users as users_db


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    key = Fernet.generate_key().decode()
    for mod in (crypto, oauth_link, main):
        monkeypatch.setattr(mod.settings, "google_token_key", key, raising=False)
    monkeypatch.setattr(main.settings, "google_client_id", "cid")
    monkeypatch.setattr(main.settings, "google_client_secret", "csec")
    monkeypatch.setattr(main.settings, "domain", "gaia.example.com")


@pytest.fixture
def client(monkeypatch, migrated):
    """Patch the module global, not main.get_pool.

    The routes reach the database through tx(), which resolves its pool by
    calling get_pool() inside gaia.core.db.pool. Patching main.get_pool leaves
    tx() pointed at the process-wide pool built from the placeholder
    DATABASE_URL, and the tests then fail on a connection error that has
    nothing to do with what they are testing.
    """
    from gaia.core.db import pool as pool_mod
    monkeypatch.setattr(pool_mod, "_pool", migrated)
    monkeypatch.setattr(main, "get_pool", lambda: migrated)
    with TestClient(main.app) as c:
        yield c


def test_start_redirects_to_google(client, ana):
    r = client.get(f"/oauth/start?t={oauth_link.mint(ana.id)}", follow_redirects=False)
    assert r.status_code == 307
    assert "accounts.google.com" in r.headers["location"]
    assert "calendar.events.owned" in r.headers["location"]


def test_start_consumes_nothing(client, ana):
    """WhatsApp fetches URLs to build link previews. If /oauth/start consumed
    the one-time token, Meta's fetcher would burn it before the developer ever
    tapped the link, and every connect would fail with nothing in the logs."""
    token = oauth_link.mint(ana.id)
    assert client.get(f"/oauth/start?t={token}", follow_redirects=False).status_code == 307
    assert client.get(f"/oauth/start?t={token}", follow_redirects=False).status_code == 307


def test_start_refuses_a_bad_token(client):
    assert client.get("/oauth/start?t=rubbish", follow_redirects=False).status_code == 403


def test_callback_refuses_an_unknown_state(client):
    assert client.get("/oauth/callback?code=x&state=nonsense").status_code == 403


async def test_callback_stores_the_grant(client, migrated, monkeypatch):
    async with migrated.connection() as c:
        from psycopg.rows import dict_row
        c.row_factory = dict_row
        user = await users_db.create_user(
            c, name="Ana", wa_id="13055558801", email="ana@gaiagroupdevelopment.com")
        await c.commit()

    monkeypatch.setattr(main, "_exchange_code", _fake_exchange("ana@gaiagroupdevelopment.com"))
    state = client.get(f"/oauth/start?t={oauth_link.mint(user.id)}",
                       follow_redirects=False).headers["location"]
    state = state.split("state=")[1].split("&")[0]

    assert client.get(f"/oauth/callback?code=x&state={state}").status_code == 200
    async with migrated.connection() as c:
        from psycopg.rows import dict_row
        c.row_factory = dict_row
        assert (await ga.get(c, user))["refresh_token"] == "1//refresh"


async def test_callback_refuses_a_different_address(client, migrated, monkeypatch):
    """The link is a bearer credential for ten minutes. A colleague who gets it
    forwarded passes the domain check -- and if this equality test were absent,
    Gaia would then create this developer's events, client names and all, on
    that colleague's calendar."""
    async with migrated.connection() as c:
        from psycopg.rows import dict_row
        c.row_factory = dict_row
        user = await users_db.create_user(
            c, name="Ana", wa_id="13055558802", email="ana@gaiagroupdevelopment.com")
        await c.commit()

    monkeypatch.setattr(main, "_exchange_code", _fake_exchange("someone@gaiagroupdevelopment.com"))
    loc = client.get(f"/oauth/start?t={oauth_link.mint(user.id)}",
                     follow_redirects=False).headers["location"]
    state = loc.split("state=")[1].split("&")[0]
    assert client.get(f"/oauth/callback?code=x&state={state}").status_code == 403


async def test_callback_refuses_a_user_with_no_address(client, migrated, monkeypatch):
    async with migrated.connection() as c:
        from psycopg.rows import dict_row
        c.row_factory = dict_row
        user = await users_db.create_user(c, name="NoMail", wa_id="13055558803")
        await c.commit()

    monkeypatch.setattr(main, "_exchange_code", _fake_exchange("nomail@gaiagroupdevelopment.com"))
    loc = client.get(f"/oauth/start?t={oauth_link.mint(user.id)}",
                     follow_redirects=False).headers["location"]
    state = loc.split("state=")[1].split("&")[0]
    assert client.get(f"/oauth/callback?code=x&state={state}").status_code == 403


def _fake_exchange(email: str):
    async def _exchange(code: str):
        return {"refresh_token": "1//refresh", "email": email,
                "scopes": "https://www.googleapis.com/auth/calendar.events.owned"}
    return _exchange


# ---------------------------------------------------------------------------
# Finding 1: a declined consent must not crash the callback with a 500.
# ---------------------------------------------------------------------------

def test_callback_declined_consent_returns_a_clean_message(client, ana):
    """Google redirects here with error=access_denied and no code when the
    developer clicks Cancel. Declining is the second most likely outcome of
    asking someone for access -- it must read as a refusal, not a crash."""
    loc = client.get(f"/oauth/start?t={oauth_link.mint(ana.id)}",
                     follow_redirects=False).headers["location"]
    state = loc.split("state=")[1].split("&")[0]
    r = client.get(f"/oauth/callback?state={state}&error=access_denied")
    assert r.status_code == 200
    assert "declined" in r.text.lower()


async def test_exchange_code_raises_cleanly_without_access_token(monkeypatch):
    """A token response with no access_token -- a revoked client, a bad code
    -- used to reach tok['access_token'] and blow up with a raw KeyError.
    _exchange_code must turn that into something the route can catch."""
    import httpx

    class _FakeResponse:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None):
            # What Google actually sends back for a bad/expired code: no
            # access_token, just an error string.
            return _FakeResponse({"error": "invalid_grant"})

        async def get(self, url, headers=None):
            raise AssertionError("must not fetch userinfo without an access_token")

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    with pytest.raises(main.OAuthExchangeError):
        await main._exchange_code("bad-code")


async def test_callback_handles_exchange_failure_without_crashing(client, migrated, monkeypatch):
    """The route-level half of the same bug: _exchange_code raising must
    surface as a clean text response, not an unhandled 500."""
    async with migrated.connection() as c:
        from psycopg.rows import dict_row
        c.row_factory = dict_row
        user = await users_db.create_user(
            c, name="Ana", wa_id="13055558804", email="ana@gaiagroupdevelopment.com")
        await c.commit()

    async def _broken_exchange(code: str) -> dict:
        raise main.OAuthExchangeError("invalid_grant")

    monkeypatch.setattr(main, "_exchange_code", _broken_exchange)
    loc = client.get(f"/oauth/start?t={oauth_link.mint(user.id)}",
                     follow_redirects=False).headers["location"]
    state = loc.split("state=")[1].split("&")[0]
    r = client.get(f"/oauth/callback?code=x&state={state}")
    assert r.status_code == 200
    assert "declined" not in r.text.lower()  # distinct from the Cancel path


# ---------------------------------------------------------------------------
# Finding 2: _PENDING_STATES must not grow without bound.
# ---------------------------------------------------------------------------

def test_start_evicts_expired_states(client, ana):
    """Every /oauth/start inserts an entry; only a completed callback removes
    one. An abandoned consent -- closed tab, declined offer -- must not sit in
    this long-lived process's memory forever."""
    stale_state = "a-state-nobody-ever-finished"
    main._PENDING_STATES[stale_state] = (str(ana.id), time.monotonic() - main._STATE_TTL_SECONDS - 1)

    client.get(f"/oauth/start?t={oauth_link.mint(ana.id)}", follow_redirects=False)

    assert stale_state not in main._PENDING_STATES


def test_callback_refuses_an_expired_state(client, ana):
    """The consent link itself is only good for ten minutes; a pending state
    older than that is already dead and must be refused, not honoured."""
    loc = client.get(f"/oauth/start?t={oauth_link.mint(ana.id)}",
                     follow_redirects=False).headers["location"]
    state = loc.split("state=")[1].split("&")[0]
    user_id, _ = main._PENDING_STATES[state]
    main._PENDING_STATES[state] = (user_id, time.monotonic() - main._STATE_TTL_SECONDS - 1)

    assert client.get(f"/oauth/callback?code=x&state={state}").status_code == 403


# ---------------------------------------------------------------------------
# Finding 3: the config.py comment says these routes refuse rather than
# half-work when the OAuth client isn't configured. Make that true.
# ---------------------------------------------------------------------------

def test_start_refuses_when_google_is_not_configured(client, ana, monkeypatch):
    monkeypatch.setattr(main.settings, "google_client_id", "")
    r = client.get(f"/oauth/start?t={oauth_link.mint(ana.id)}", follow_redirects=False)
    assert r.status_code == 503
