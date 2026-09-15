"""The one-time link Gaia sends over WhatsApp to start a Google consent flow.

Consent happens in a browser; Gaia knows people only by wa_id. Nothing links
the two, and this token is that link: signed, bound to one user_id, and valid
for ten minutes.

Domain-separated from crypto.py's use of the same secret by the HMAC prefix
below -- the key encrypts refresh tokens there and authenticates link payloads
here, and those must never be interchangeable.
"""

import base64
import hmac
import json
import time
from hashlib import sha256
from uuid import UUID

from gaia.core.config import settings

_PREFIX = b"gaia-oauth-link-v1"


def _sign(body: bytes) -> str:
    if not settings.google_token_key:
        raise RuntimeError("GOOGLE_TOKEN_KEY is not set; cannot sign a consent link.")
    mac = hmac.new(_PREFIX + settings.google_token_key.encode(), body, sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mint(user_id: UUID, *, ttl_seconds: int = 600) -> str:
    body = json.dumps({"u": str(user_id), "e": int(time.time()) + ttl_seconds}).encode()
    return f"{_b64(body)}.{_sign(body)}"


def verify(token: str) -> UUID | None:
    """None for anything wrong -- expired, tampered, malformed. The caller
    cannot usefully distinguish them and neither can the person holding the
    link, so they all get the same answer."""
    try:
        encoded, sig = token.split(".", 1)
        body = _unb64(encoded)
        if not hmac.compare_digest(sig, _sign(body)):
            return None
        claims = json.loads(body)
        if int(claims["e"]) < time.time():
            return None
        return UUID(claims["u"])
    except Exception:
        return None
