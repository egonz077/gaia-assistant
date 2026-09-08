import asyncio

from gaia.core.turns import TurnQueue


async def test_a_burst_becomes_one_turn(ana):
    seen = []

    async def handler(user, batch):
        seen.append([m["text"] for m in batch])

    q = TurnQueue(handler, debounce=0.05)
    await q.submit(ana, {"text": "notes"})
    await q.submit(ana, {"text": "photo"})
    await q.submit(ana, {"text": "book Tuesday"})
    await q.drain()

    assert seen == [["notes", "photo", "book Tuesday"]]


async def test_messages_arriving_mid_turn_queue_rather_than_racing(ana):
    running = []
    concurrent = []

    async def handler(user, batch):
        concurrent.append(len(running))
        running.append(1)
        await asyncio.sleep(0.05)
        running.pop()

    q = TurnQueue(handler, debounce=0.01)
    await q.submit(ana, {"text": "first"})
    await asyncio.sleep(0.02)          # let the first turn start
    await q.submit(ana, {"text": "second"})
    await q.drain()

    assert concurrent == [0, 0], "two turns ran concurrently for one user"


async def test_different_users_are_not_serialised(ana, sofia):
    order = []

    async def handler(user, batch):
        order.append(user.name)
        await asyncio.sleep(0.05)

    q = TurnQueue(handler, debounce=0.01)
    await q.submit(ana, {"text": "a"})
    await q.submit(sofia, {"text": "b"})
    await q.drain()

    assert sorted(order) == ["Ana", "Sofia"]
