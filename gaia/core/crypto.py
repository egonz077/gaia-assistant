"""Encryption for credentials at rest. Only refresh tokens use it today."""

from cryptography.fernet import Fernet

from gaia.core.config import settings


def _cipher() -> Fernet:
    if not settings.google_token_key:
        raise RuntimeError(
            "GOOGLE_TOKEN_KEY is not set. Refusing to store a credential in "
            "plaintext -- generate a key with Fernet.generate_key()."
        )
    return Fernet(settings.google_token_key.encode())


def encrypt(plaintext: str) -> str:
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _cipher().decrypt(token.encode()).decode()
