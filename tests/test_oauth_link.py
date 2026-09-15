"""The token Gaia puts in a WhatsApp link. It is a bearer credential for its
whole lifetime, so it is signed, bound to exactly one user, and short."""

import time
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest

from gaia.core import oauth_link


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(oauth_link.settings, "google_token_key", Fernet.generate_key().decode())


def test_round_trip():
    uid = uuid4()
    assert oauth_link.verify(oauth_link.mint(uid)) == uid


def test_expired_token_is_refused():
    assert oauth_link.verify(oauth_link.mint(uuid4(), ttl_seconds=-1)) is None


def test_tampered_payload_is_refused():
    """Swapping the user id must not survive the signature -- otherwise anyone
    holding one link could bind an account to any user they liked."""
    token = oauth_link.mint(uuid4())
    body, sig = token.split(".", 1)
    forged = oauth_link.mint(uuid4()).split(".", 1)[0]
    assert oauth_link.verify(f"{forged}.{sig}") is None


def test_garbage_is_refused():
    for junk in ["", "nodot", "a.b", "...."]:
        assert oauth_link.verify(junk) is None


def test_signature_comparison_is_constant_time():
    """hmac.compare_digest, not ==. A timing oracle on this signature is a
    forged link, and a forged link is someone else's calendar."""
    import inspect
    assert "compare_digest" in inspect.getsource(oauth_link.verify)
