import pytest

from gaia.core import crypto


def test_round_trip(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setattr(crypto.settings, "google_token_key", Fernet.generate_key().decode())
    assert crypto.decrypt(crypto.encrypt("1//refresh-token")) == "1//refresh-token"


def test_ciphertext_is_not_the_plaintext(monkeypatch):
    """The point of the exercise. A database dump that leaks must not hand
    over live Google credentials -- the key lives in .env, not in the dump."""
    from cryptography.fernet import Fernet
    monkeypatch.setattr(crypto.settings, "google_token_key", Fernet.generate_key().decode())
    assert "1//refresh-token" not in crypto.encrypt("1//refresh-token")


def test_missing_key_fails_loudly_at_use_not_import(monkeypatch):
    """Empty by default so a checkout with no Google feature configured still
    imports and boots -- same reasoning as deepgram_api_key. It must then fail
    visibly rather than storing something reversible."""
    monkeypatch.setattr(crypto.settings, "google_token_key", "")
    with pytest.raises(RuntimeError, match="GOOGLE_TOKEN_KEY"):
        crypto.encrypt("anything")
