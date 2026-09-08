import asyncio

import voyageai

from gaia.core.config import settings

MODEL = "voyage-3.5-lite"
# Must match vector(N) in migrations/001_init.sql (memory_chunks.embedding).
# A test pins this against the live schema, but that only catches drift
# between this constant and the DB column — it cannot catch MODEL being
# pointed at a Voyage model whose real output width differs, since verifying
# that would require calling the real API.
EMBED_DIM = 1024

_client = voyageai.Client(api_key=settings.voyage_api_key)


async def embed(texts: list[str], input_type: str = "document") -> list[list[float]]:
    """The Voyage client is synchronous; keep it off the event loop."""
    result = await asyncio.to_thread(
        _client.embed, texts, model=MODEL, input_type=input_type
    )
    return result.embeddings
