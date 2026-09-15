"""One developer's Google grant.

Ownership-scoped throughout, and deliberately so: a token answers *whose
account is this*, which is responsibility, never *who may see this*. There is
no visibility setting at which a colleague's refresh token is readable, which
is why the table has no visibility column at all.
"""

from gaia.core import crypto
from gaia.core.models import User


async def upsert(conn, user: User, *, google_email: str, refresh_token: str, scopes: str) -> None:
    """Re-consenting replaces everything, including revoked_at and
    revoked_notified_at. A reconnect that left either behind would look like
    it worked and then behave as though it had not -- and a revocation after
    THIS connection is a new event, which a stale notified flag would
    silence forever."""
    await conn.execute(
        """INSERT INTO google_accounts (user_id, google_email, refresh_token_enc, scopes)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (user_id) DO UPDATE
             SET google_email        = EXCLUDED.google_email,
                 refresh_token_enc   = EXCLUDED.refresh_token_enc,
                 scopes              = EXCLUDED.scopes,
                 granted_at          = now(),
                 revoked_at          = NULL,
                 revoked_notified_at = NULL""",
        (user.id, google_email, crypto.encrypt(refresh_token), scopes),
    )


async def get(conn, user: User) -> dict | None:
    cur = await conn.execute(
        """SELECT google_email, refresh_token_enc, scopes, revoked_at, revoked_notified_at
           FROM google_accounts WHERE user_id = %s""",
        (user.id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return {
        "google_email": row["google_email"],
        "refresh_token": crypto.decrypt(row["refresh_token_enc"]),
        "scopes": row["scopes"],
        "revoked_at": row["revoked_at"],
        "revoked_notified_at": row["revoked_notified_at"],
    }


async def revoke(conn, user: User) -> None:
    """Marked, not deleted. The digest says so once; deleting the row would
    lose the fact that a grant ever existed, and with it the ability to tell
    'never connected' from 'connection lapsed'."""
    await conn.execute(
        "UPDATE google_accounts SET revoked_at = now() WHERE user_id = %s", (user.id,)
    )


async def mark_revoked_notified(conn, user: User) -> None:
    """Stamped the moment the digest actually says the grant is gone.

    Replaces an earlier attempt to derive "have we told them" from
    last_digest_on, a DATE compared against revoked_at: written after a
    successful send, it either re-announced the next morning (revocation and
    digest on the same calendar day) or silently ate a revocation that
    happened later that same morning (a strict '>'). A DATE can only answer
    "what day is it", not "have we said this" -- this column is that fact,
    on its own.
    """
    await conn.execute(
        "UPDATE google_accounts SET revoked_notified_at = now() WHERE user_id = %s", (user.id,)
    )
