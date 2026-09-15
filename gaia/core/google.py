"""Authenticated calls to Google, on behalf of one developer.

The httpx client is a required parameter, not something this module creates.
That is the seam tests use -- the alternative is an env var only tests set,
which this codebase does not do (see usage.record, which takes `pool` for
exactly this reason).
"""

import logging

from gaia.core.config import settings
from gaia.core.db import google_accounts as ga_db
from gaia.core.models import User

log = logging.getLogger("gaia.google")


class RevokedGrant(Exception):
    """No usable grant: never connected, or revoked in the developer's Google
    account. Callers turn this into an offer of a fresh link, never a retry."""


async def access_token(conn, user: User, *, http) -> str:
    account = await ga_db.get(conn, user)
    if account is None or account["revoked_at"] is not None:
        raise RevokedGrant(f"no live Google grant for {user.id}")

    resp = await http.post("https://oauth2.googleapis.com/token", data={
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "refresh_token": account["refresh_token"],
        "grant_type": "refresh_token",
    })
    body = resp.json()
    if resp.status_code != 200 or "access_token" not in body:
        # invalid_grant is the documented signal for a revoked or expired
        # refresh token. Marking it here means the digest can say so once,
        # instead of failing quietly every morning.
        if body.get("error") == "invalid_grant":
            await ga_db.revoke(conn, user)
            raise RevokedGrant(f"grant revoked for {user.id}")
        raise RuntimeError(f"token refresh failed: {resp.status_code} {body}")
    return body["access_token"]


async def request(conn, user: User, method: str, url: str, *, http, json=None) -> dict:
    token = await access_token(conn, user, http=http)
    resp = await http.request(
        method, url, headers={"Authorization": f"Bearer {token}"}, json=json
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"google {method} {url} -> {resp.status_code} {resp.text[:300]}")
    return resp.json() if resp.content else {}
