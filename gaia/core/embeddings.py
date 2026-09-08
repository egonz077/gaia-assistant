import asyncio

import voyageai

from gaia.core.config import settings

MODEL = "voyage-3.5-lite"  # 1024 dims, matching vector(1024) in the schema

_client = voyageai.Client(api_key=settings.voyage_api_key)


async def embed(texts: list[str], input_type: str = "document") -> list[list[float]]:
    """The Voyage client is synchronous; keep it off the event loop."""
    result = await asyncio.to_thread(
        _client.embed, texts, model=MODEL, input_type=input_type
    )
    return result.embeddings
