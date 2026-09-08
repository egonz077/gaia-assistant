import asyncio
import logging
from collections import defaultdict

from gaia.core.models import User

log = logging.getLogger("gaia.turns")


class TurnQueue:
    """One turn at a time, per user, with a debounce window.

    People send three messages in five seconds — notes, a photo, then "oh and
    book Tuesday". Without this each becomes its own agent loop: interleaved
    tool calls, three overlapping replies, racing writes to the same contact.
    """

    def __init__(self, handler, debounce: float = 3.0):
        self._handler = handler
        self._debounce = debounce
        self._pending: dict[str, list[dict]] = defaultdict(list)
        self._timers: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._running: set[asyncio.Task] = set()

    async def submit(self, user: User, message: dict) -> None:
        key = user.wa_id
        self._pending[key].append(message)

        if timer := self._timers.get(key):
            timer.cancel()
        self._timers[key] = asyncio.create_task(self._fire_after_debounce(user))

    async def _fire_after_debounce(self, user: User) -> None:
        key = user.wa_id
        try:
            await asyncio.sleep(self._debounce)
        except asyncio.CancelledError:
            return  # a newer message joined the burst

        batch = self._pending.pop(key, [])
        self._timers.pop(key, None)
        if not batch:
            return

        task = asyncio.create_task(self._run(user, batch))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _run(self, user: User, batch: list[dict]) -> None:
        async with self._locks[user.wa_id]:
            try:
                await self._handler(user, batch)
            except Exception:
                log.exception("turn failed for user %s", user.id)

    async def drain(self) -> None:
        """Test helper: wait for every debounce timer and turn to finish."""
        while self._timers or self._running:
            await asyncio.gather(*list(self._timers.values()), return_exceptions=True)
            await asyncio.gather(*list(self._running), return_exceptions=True)
