import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response

from gaia.core import whatsapp
from gaia.core.config import settings
from gaia.core.db import messages as messages_db
from gaia.core.db import users as users_db
from gaia.core.db.migrate import run_migrations
from gaia.core.db.pool import get_pool, tx
from gaia.core.turns import TurnQueue
from gaia.butler import handle_turn

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
            if await messages_db.seen(conn, message["id"]):
                continue
        await queue.submit(user, message)

    # Returns before the agent loop runs. Meta retries slow webhooks and
    # eventually disables the subscription over them.
    return {"status": "ok"}
