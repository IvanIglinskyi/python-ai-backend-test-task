"""Service-wide LLM rate limit stored in PostgreSQL.

Every provider call inserts a row into `llm_calls`. A slot is granted while
fewer than `limit` rows exist in the last 60 seconds. A transaction-scoped
advisory lock serialises the check-and-insert, so all worker replicas share
one budget.
"""

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_LOCK_KEY = 7_311_042  # arbitrary constant for pg_advisory_xact_lock
WINDOW_SECONDS = 60


async def try_acquire(session: AsyncSession, limit: int) -> float:
    """Take a slot. Returns 0 on success, otherwise seconds to wait before retrying."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    row = (
        await session.execute(
            text(
                "SELECT count(*) AS used, min(called_at) AS oldest, now() AS now "
                "FROM llm_calls WHERE called_at > now() - make_interval(secs => :w)"
            ),
            {"w": WINDOW_SECONDS},
        )
    ).one()
    if row.used < limit:
        await session.execute(text("INSERT INTO llm_calls (called_at) VALUES (now())"))
        await session.execute(
            text("DELETE FROM llm_calls WHERE called_at < now() - interval '1 hour'")
        )
        await session.commit()
        return 0.0
    await session.rollback()
    wait = WINDOW_SECONDS - (row.now - row.oldest).total_seconds()
    return max(wait, 0.05)


async def acquire(
    factory: async_sessionmaker[AsyncSession],
    limit: int,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Block until a provider call is allowed."""
    while True:
        async with factory() as session:
            wait = await try_acquire(session, limit)
        if wait == 0:
            return
        await sleep(min(wait, 5.0))
