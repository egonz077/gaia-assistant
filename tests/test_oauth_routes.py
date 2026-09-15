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
