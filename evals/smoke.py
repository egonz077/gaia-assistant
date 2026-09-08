"""Live end-to-end smoke test against the real Anthropic API.

Costs real money (a few cents). Not part of the pytest suite — nothing here is
mocked except embeddings, because VOYAGE_API_KEY is not yet provisioned.

    .venv/bin/python -m evals.smoke

What this actually proves, none of which the unit suite can:
  - the model id in .env is real and accepted
  - the request shape is valid: max_tokens, output_config, system blocks
    carrying cache_control
  - all registered tool schemas are accepted by the API
  - the agent loop runs a real tool call and feeds the result back
  - what the assistant actually SAYS — the system prompt has never been read
    by a model before this
"""

import asyncio
import os
import pathlib
import sys

# Load .env into the process environment. In production, compose `env_file:`
# does this; the SDK then resolves credentials itself from the environment,
# which is why the client below is constructed zero-arg.
for line in pathlib.Path(".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.split("#")[0].strip())

from anthropic import AsyncAnthropic  # noqa: E402

import gaia.capabilities  # noqa: F401,E402  — populates the registry
from gaia.capabilities.base import registry  # noqa: E402
from gaia.core.config import settings  # noqa: E402
from gaia.core.db import users as users_db  # noqa: E402
from gaia.core.db.migrate import run_migrations  # noqa: E402
from gaia.core.db.pool import get_pool, tx  # noqa: E402
from gaia.core import llm  # noqa: E402

BASE_PROMPT = """You are the assistant for {name}, an agent at the Gaia real-estate \
company, reachable over WhatsApp.

When she sends meeting notes — typed or photographed handwriting — transcribe if needed, \
then extract a short summary, the people involved, commitments made, and any follow-up \
dates. Save them with your tools. Echo back what you understood and ask her to confirm \
anything ambiguous: names, numbers, dates.

Answer questions about past meetings, leads and contacts using your tools. Never contact \
third parties.

Style: brief and warm, like a text message. No markdown headers or bullet lists.

Today is 2026-09-08 in her timezone (America/New_York).
People she has worked with recently: (nobody yet)
Use lookup_contact for details on any of them.
"""


async def fake_embed(texts, input_type="document"):
    """VOYAGE_API_KEY is still a placeholder. Embeddings stay stubbed."""
    from gaia.core.embeddings import EMBED_DIM
    return [[0.1] + [0.0] * (EMBED_DIM - 1) for _ in texts]


SCENARIOS = [
    ("plain question, expect NO tool call",
     "hey, anything I need to follow up on today?"),
    ("meeting notes, expect save_meeting + create_lead",
     "Just showed the Coral Gables place to Maria Delgado. She loved the kitchen, "
     "hated the pool. Budget tops out around 600k. I said I'd send comps by Friday."),
    ("ambiguity, expect a clarifying question rather than a guess",
     "met with the Riveras, they want to list. asking 1.2 or maybe 1.4, I couldn't "
     "read my own writing"),
]


async def main() -> int:
    import gaia.core.db.memory as memory_mod
    memory_mod.embed = fake_embed  # noqa: patched before any tool runs

    pool = get_pool()
    await pool.open(wait=True)
    await run_migrations(pool)

    async with tx(pool) as conn:
        user = await users_db.get_by_wa_id(conn, "13055550001")
        if user is None:
            user = await users_db.create_user(conn, name="Ana", wa_id="13055550001")

    client = AsyncAnthropic()  # zero-arg: SDK resolves creds from the environment
    tool_defs = registry.tool_defs(user)
    print(f"model      : {settings.model}")
    print(f"tools sent : {[t['name'] for t in tool_defs]}")
    print("=" * 72)

    failures = 0
    for label, text in SCENARIOS:
        print(f"\n### {label}\n>>> {text}\n")
        messages = [{"role": "user", "content": [{"type": "text", "text": text}]}]
        try:
            async with tx(pool) as conn:
                reply = await llm.run_agent(
                    client, conn, user, messages,
                    BASE_PROMPT.format(name=user.name), tool_defs,
                )
            marker = "  (FALLBACK — model produced nothing usable)" if reply == llm.FALLBACK_TEXT else ""
            print(f"<<< {reply}{marker}")
            if reply == llm.FALLBACK_TEXT:
                failures += 1
        except Exception as exc:
            print(f"!!! FAILED: {type(exc).__name__}: {exc}")
            failures += 1

    async with tx(pool) as conn:
        for table in ("meetings", "leads", "contacts", "commitments", "memory_chunks"):
            n = (await (await conn.execute(f"SELECT count(*) AS n FROM {table}")).fetchone())["n"]
            print(f"{table:<15} rows: {n}")

    await pool.close()
    print("\n" + ("SMOKE FAILED" if failures else "SMOKE PASSED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
