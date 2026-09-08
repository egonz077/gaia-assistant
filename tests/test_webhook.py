import asyncio
import hashlib
import hmac
import json
import threading
import time

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, migrated):
    from gaia.core import whatsapp
    from gaia.core.db import pool as pool_module

    monkeypatch.setattr(whatsapp.settings, "wa_app_secret", "shh")
    monkeypatch.setattr(pool_module, "_pool", migrated)

    from gaia.main import app

    with TestClient(app) as c:
        yield c


@pytest_asyncio.fixture
async def wa_user(migrated):
    """A user committed on its own connection, independent of any `conn`
    fixture — the app resolves the sender on a different pooled connection,
    which must not see an uncommitted row sitting in another session."""
    from gaia.core.db import users as users_db
    from gaia.core.db.pool import tx

    async with tx(migrated) as c:
        return await users_db.create_user(c, name="Known", wa_id="13055551234")


def _signed(client, payload: dict, secret: bytes = b"shh"):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=body, headers={"x-hub-signature-256": sig})


def test_bad_signature_is_rejected(client):
    body = json.dumps({"entry": []}).encode()
    r = client.post("/webhook", content=body, headers={"x-hub-signature-256": "sha256=bad"})
    assert r.status_code == 403


def test_missing_signature_is_rejected(client):
    assert client.post("/webhook", content=b"{}").status_code == 403


def test_verification_challenge_is_echoed(client, monkeypatch):
    from gaia.core.config import settings

    monkeypatch.setattr(settings, "wa_verify_token", "letmein")
    r = client.get("/webhook", params={"hub.verify_token": "letmein", "hub.challenge": "42"})
    assert r.status_code == 200
    assert r.text == "42"


def test_wrong_verify_token_is_rejected(client):
    r = client.get("/webhook", params={"hub.verify_token": "nope", "hub.challenge": "42"})
    assert r.status_code == 403


def test_unknown_sender_is_accepted_but_ignored(client):
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.x", "from": "19998887777", "type": "text", "text": {"body": "hi"}}
    ]}}]}]}
    assert _signed(client, payload).status_code == 200


def test_health_reports_ok(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_webhook_returns_before_the_turn_finishes(client, wa_user, monkeypatch):
    """The 200 must come back before the agent loop runs — Meta retries slow
    webhooks and eventually disables the subscription over them.

    A handler that ran inline (instead of being handed to the debounced
    queue) would make this test fail two independent ways: the request would
    take >= HANDLER_DELAY to return, and `finished` would already be set by
    the time the response body is read. Passing both is what proves ordering
    rather than being a coincidence of timing.
    """
    from gaia.main import queue

    HANDLER_DELAY = 0.2
    started = threading.Event()
    finished = threading.Event()

    async def slow_handler(user, batch):
        started.set()
        await asyncio.sleep(HANDLER_DELAY)
        finished.set()

    monkeypatch.setattr(queue, "_handler", slow_handler)
    monkeypatch.setattr(queue, "_debounce", 0.01)

    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.ordering", "from": wa_user.wa_id, "type": "text", "text": {"body": "hi"}}
    ]}}]}]}

    t0 = time.monotonic()
    r = _signed(client, payload)
    elapsed = time.monotonic() - t0

    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert elapsed < HANDLER_DELAY * 0.75, (
        f"webhook took {elapsed:.3f}s to respond — looks like it waited on the turn"
    )
    assert not finished.is_set(), "the turn had already finished when the response came back"

    deadline = time.monotonic() + 2.0
    while not finished.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started.is_set() and finished.is_set(), "the debounced turn never ran at all"


def _drain(queue, timeout: float = 3.0) -> None:
    """Wait out every debounce timer and running turn, then settle.

    Waiting on `_running` alone is not enough: a queued redelivery sits in
    `_timers` until its debounce fires, so a turn that should never have been
    created would not have been created *yet* when the assertion ran.
    """
    deadline = time.monotonic() + timeout
    while (queue._timers or queue._running) and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)


def test_a_redelivered_message_is_a_no_op(client, wa_user, monkeypatch):
    """Spec §9.2 requires this row and it was the one line of it never
    written — and the one with a bug.

    `seen` was checked at the webhook, but the row backing it was only written
    inside handle_turn, after the debounce *and* after the per-user turn lock.
    The gap was therefore not 3 seconds but the whole of any turn already in
    flight — 10-30s on a photo, squarely inside Meta's retry window. A
    redelivery in that gap passed the check, was queued as a fresh burst, and
    ran as a second turn: the model saw the same message twice and she got two
    replies to one.

    Reproduced here by holding the first turn open while the redelivery
    arrives, which is exactly the shape Meta retries into.
    """
    from gaia.main import queue

    turns = []
    release = threading.Event()

    async def slow_handler(user, batch):
        turns.append([m["id"] for m in batch])
        while not release.is_set():
            await asyncio.sleep(0.01)

    monkeypatch.setattr(queue, "_handler", slow_handler)
    monkeypatch.setattr(queue, "_debounce", 0.01)

    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.dupe", "from": wa_user.wa_id, "type": "text", "text": {"body": "hi"}}
    ]}}]}]}

    assert _signed(client, payload).status_code == 200

    deadline = time.monotonic() + 2.0
    while not turns and time.monotonic() < deadline:
        time.sleep(0.01)
    assert turns, "the first turn never started"

    # Meta redelivers while that turn is still running.
    assert _signed(client, payload).status_code == 200
    release.set()

    _drain(queue)
    assert turns == [["wamid.dupe"]], "the redelivery ran as a second turn"


def test_a_redelivery_after_the_turn_finishes_is_also_a_no_op(client, wa_user, monkeypatch):
    from gaia.main import queue

    turns = []

    async def handler(user, batch):
        turns.append([m["id"] for m in batch])

    monkeypatch.setattr(queue, "_handler", handler)
    monkeypatch.setattr(queue, "_debounce", 0.01)

    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.late", "from": wa_user.wa_id, "type": "text", "text": {"body": "hi"}}
    ]}}]}]}

    _signed(client, payload)
    deadline = time.monotonic() + 2.0
    while not turns and time.monotonic() < deadline:
        time.sleep(0.01)

    _signed(client, payload)
    _drain(queue)
    assert turns == [["wamid.late"]]
