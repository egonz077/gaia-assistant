import base64
import json
import logging
import time
import urllib.parse
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response

from gaia.core import oauth_link, whatsapp
from gaia.core.config import settings
from gaia.core.db import google_accounts as ga_db
from gaia.core.db import users as users_db
from gaia.core.db.migrate import run_migrations
from gaia.core.db.pool import get_pool, tx
from gaia.core.turns import TurnQueue
from gaia.butler import handle_turn, receive

import gaia.capabilities  # noqa: F401  — importing populates the registry

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gaia")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # @app.on_event("startup") is deprecated in FastAPI 0.141+; lifespan is
    # the supported replacement.
    pool = get_pool()
    await pool.open(wait=True)
    applied = await run_migrations(pool)
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    yield


app = FastAPI(lifespan=lifespan)
wa = whatsapp.WhatsAppClient()


async def _handler(user, batch):
    await handle_turn(user, batch, wa)


queue = TurnQueue(_handler, debounce=settings.debounce_seconds)


@app.get("/health")
async def health() -> dict:
    async with tx() as conn:
        await conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/webhook")
async def verify(request: Request) -> Response:
    params = request.query_params
    if params.get("hub.verify_token") == settings.wa_verify_token:
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(status_code=403)


@app.post("/webhook")
async def inbound(request: Request) -> dict:
    body = await request.body()
    if not whatsapp.verify_signature(body, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(status_code=403)

    payload = await request.json()
    for message in whatsapp.parse_messages(payload):
        async with tx() as conn:
            user = await users_db.get_by_wa_id(conn, message["from"])
            if user is None:
                log.info("ignoring message from unknown number %s", message["from"])
                continue
            # Logged here, in the same transaction as the dedup check and
            # before the message is queued — not inside the turn, which may
            # not start for the length of a whole preceding turn. See
            # butler.receive.
            if not await receive(conn, user, message):
                log.info("ignoring redelivery of message %s", message["id"])
                continue
        await queue.submit(user, message)

    # Returns before the agent loop runs. Meta retries slow webhooks and
    # eventually disables the subscription over them.
    return {"status": "ok"}


CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.owned"

# openid is what makes the token response carry an id_token, and the id_token
# is the only thing in this flow that says who consented: /oauth2/v2/userinfo
# is served only to tokens holding openid, email or profile, so a token with
# the calendar scope alone gets a 403 there and the callback learns nothing.
# Both are non-sensitive -- unlike the Gmail scopes in research 2 -- so they
# cost the app's Internal configuration, and its verification exemption,
# nothing at all.
OAUTH_SCOPES = ("openid", "email", CALENDAR_SCOPE)

# Matches oauth_link.mint's own default TTL. A pending state can't legitimately
# outlive the consent link that created it, so evicting anything older costs a
# live consent nothing -- and refusing anything older closes the gap between
# "will be evicted eventually" and "is actually still honoured".
_STATE_TTL_SECONDS = 600

# state -> (user_id, inserted_at). In-process because a restart mid-consent is
# a retry, not a data-loss event: the developer taps the link again. A table
# would outlive the thing it describes. Evicted lazily on each insert (see
# oauth_start) so an abandoned consent -- closed tab, declined offer -- does
# not sit in this long-lived process's memory forever.
_PENDING_STATES: dict[str, tuple[str, float]] = {}


def _evict_expired_states() -> None:
    now = time.monotonic()
    for s, (_, created) in list(_PENDING_STATES.items()):
        if now - created > _STATE_TTL_SECONDS:
            del _PENDING_STATES[s]


class OAuthExchangeError(Exception):
    """The exchange did not yield a usable grant -- a non-200 from the token
    endpoint, a revoked client, a reused or expired code, an id_token that
    will not decode. Without this, the lookup below (tok['access_token'])
    raises a bare KeyError that FastAPI turns into an unhandled 500; this lets
    the callback show a clean refusal instead, and say why in the log."""


def _id_token_claims(id_token: str) -> dict:
    """The id_token's payload, with its signature deliberately not verified.

    Google documents the exemption precisely: a token received "directly from
    Google" over HTTPS, in response to our own client-authenticated request,
    needs no signature validation, because the channel already establishes who
    sent it. That is exactly this code path and nothing else. The exemption
    would NOT hold for an id_token arriving any other way -- posted to a
    webhook, forwarded by a browser, read out of a header someone else could
    write -- where a JWKS lookup is the only thing standing between us and a
    forged `hd`. Anyone reusing this helper for such a token is reusing the
    exemption too, and it does not travel.
    """
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # JWTs drop base64 padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as exc:
        raise OAuthExchangeError(f"id_token could not be decoded: {exc}") from exc


async def _exchange_code(code: str) -> dict:
    """Swap an authorization code for a refresh token and the account's identity.

    Separated so tests can replace it: the alternative is an env var only tests
    set, which this codebase does not do.
    """
    import httpx

    redirect = f"https://{settings.domain}/oauth/callback"
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": redirect,
            "grant_type": "authorization_code",
        })
    # Read before parsing. A scope mismatch, a rotated secret or a proxy's HTML
    # error page all answer non-200, and parsing regardless turned Google
    # saying no into an empty address and a refusal that blamed the developer's
    # own account -- with nothing in the logs, because nothing checked.
    if resp.status_code != 200:
        raise OAuthExchangeError(f"token endpoint returned {resp.status_code}: {resp.text[:300]}")
    tok = resp.json()
    if "access_token" not in tok:
        raise OAuthExchangeError(tok.get("error", "no access_token in token response"))

    # The identity comes from the token response itself. The second call this
    # replaces went to /oauth2/v2/userinfo, which our scopes are not served by.
    claims = _id_token_claims(tok.get("id_token", ""))
    return {"refresh_token": tok.get("refresh_token", ""),
            "email": claims.get("email", ""),
            # Google's own assertion of the account's Workspace domain. A
            # consumer account carries no hd claim at all.
            "hd": claims.get("hd", ""),
            "scopes": tok.get("scope", "")}


@app.get("/oauth/start")
async def oauth_start(t: str = "") -> Response:
    """Validates and redirects. Consumes NOTHING.

    WhatsApp builds link previews by fetching URLs. If this consumed the
    one-time token, Meta's fetcher would burn it before the developer ever
    tapped the link and every connect attempt would fail, with nothing in the
    logs to explain it. A preview fetcher gets a 307 to Google and achieves
    nothing, because consent needs a human.
    """
    user_id = oauth_link.verify(t)
    if user_id is None:
        raise HTTPException(status_code=403, detail="This link has expired. Ask Gaia for a new one.")

    if not settings.google_client_id or not settings.google_client_secret:
        # Matches the comment on Settings.google_client_id: a checkout with no
        # Workspace integration configured still boots, but these routes must
        # actually refuse rather than hand the developer Google's error page
        # for a redirect built with an empty client_id.
        raise HTTPException(status_code=503, detail="Google Workspace integration is not configured.")

    import secrets
    _evict_expired_states()
    state = secrets.token_urlsafe(24)
    _PENDING_STATES[state] = (str(user_id), time.monotonic())
    query = urllib.parse.urlencode({
        "client_id": settings.google_client_id,
        "redirect_uri": f"https://{settings.domain}/oauth/callback",
        "response_type": "code",
        "scope": " ".join(OAUTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        # Additive, so the email increment's scopes join this grant rather than
        # replacing it and silently dropping calendar access.
        "include_granted_scopes": "true",
    })
    return Response(status_code=307,
                    headers={"location": f"https://accounts.google.com/o/oauth2/v2/auth?{query}"})


@app.get("/oauth/callback")
async def oauth_callback(code: str = "", state: str = "", error: str = "") -> Response:
    entry = _PENDING_STATES.pop(state, None)
    if entry is None:
        raise HTTPException(status_code=403, detail="That consent did not come from this server.")
    user_id, created_at = entry
    if time.monotonic() - created_at > _STATE_TTL_SECONDS:
        raise HTTPException(status_code=403, detail="This consent link has expired. Ask Gaia for a new one.")

    if error:
        # Declining is the second most likely outcome of asking someone for
        # access, right after granting it. Google redirects here with
        # error=access_denied and no code when the developer clicks Cancel;
        # that must read as a refusal, not a crash from a doomed exchange.
        return Response(content="Google consent was declined. Ask Gaia for a new link to try again.",
                        media_type="text/plain")

    try:
        result = await _exchange_code(code)
    except OAuthExchangeError as exc:
        # Logged, because the failure the developer sees is generic by design
        # and the cause is not: a scope mismatch and a reused code look
        # identical from the browser.
        log.warning("Google code exchange failed for user %s: %s", user_id, exc)
        return Response(content="Google did not return an access grant. Ask Gaia for a new link and try again.",
                        media_type="text/plain")

    async with tx() as conn:
        user = await users_db.get_by_id(conn, user_id)
        if user is None:
            raise HTTPException(status_code=403)
        expected = await users_db.get_email(conn, user)
        # Both checks, in this order. The domain is what keeps the app's
        # Internal configuration true; the equality is what stops a forwarded
        # link binding a colleague's account to this developer's identity.
        #
        # The domain is read from the id_token's `hd` claim rather than from
        # the address's suffix. Google signs hd and says so -- "the value can
        # be trusted" -- where an address is a string that merely ends in
        # something. A missing claim is a refusal in its own right, not an ""
        # that happens to compare false: nothing about an empty string belongs
        # in a decision about who consented.
        if not expected or not result["email"] or not result["hd"]:
            raise HTTPException(status_code=403, detail="That account cannot be connected.")
        if result["hd"] != settings.google_domain:
            raise HTTPException(status_code=403, detail="That account cannot be connected.")
        if result["email"].lower() != expected.lower():
            raise HTTPException(status_code=403, detail="That is not the account we expected.")
        await ga_db.upsert(conn, user, google_email=result["email"],
                           refresh_token=result["refresh_token"], scopes=result["scopes"])
    return Response(content="Calendar connected. You can close this tab.", media_type="text/plain")
