"""Serverless worker: drain the queue for a bounded time, then return.

On Vercel there is no long-running process. `/internal/tick` calls `drain()`,
which runs the same claim/process code as the worker container until the queue
is empty or the time budget is spent. Leases in PostgreSQL make overlapping
ticks safe: SKIP LOCKED gives every job and delivery to one tick only, and a
tick killed mid-job leaves a lease that expires and lets another tick retry.
"""

import asyncio
import logging
import time

import httpx
from sqlalchemy import text

from app.config import Settings
from app.db.session import get_session_factory, init_models
from app.services import delivery, drafting

log = logging.getLogger("tick")

_KICK = text(
    """
    INSERT INTO worker_kicks (id, kicked_at) VALUES (1, now())
    ON CONFLICT (id) DO UPDATE SET kicked_at = now()
     WHERE worker_kicks.kicked_at < now() - make_interval(secs => :interval)
    RETURNING id
    """
)


_NEXT_DUE = {
    "drafts": text(
        "SELECT EXTRACT(EPOCH FROM min(available_at) - now()) FROM jobs "
        "WHERE kind = 'generate_draft' AND status IN ('queued', 'running')"
    ),
    "deliveries": text(
        "SELECT EXTRACT(EPOCH FROM min(available_at) - now()) FROM deliveries "
        "WHERE state IN ('pending', 'in_flight')"
    ),
}


async def next_due(kind: str) -> float | None:
    """Seconds until the next draft job or delivery becomes claimable; None if there is none."""
    async with get_session_factory()() as session:
        value = await session.scalar(_NEXT_DUE[kind])
    return None if value is None else max(float(value), 0.0)


def _draft_guard(settings: Settings) -> float:
    """Seconds one draft may take; do not start a job with less time left."""
    if settings.llm_api_key:
        return settings.llm_timeout_seconds + 10
    return settings.llm_stub_max_delay + settings.llm_timeout_seconds / 4 + 5


async def drain(
    settings: Settings,
    budget_seconds: float | None = None,
    client: httpx.AsyncClient | None = None,
    base_url: str | None = None,
) -> dict:
    """Process due work; wait for retries that fall due within the budget.

    When work is still pending at the end (a backoff longer than the budget,
    or a lease held by another tick), the tick starts the next one via kick().
    """
    await init_models()
    budget = budget_seconds if budget_seconds is not None else settings.tick_seconds
    started = time.monotonic()
    stats = {"drafts": 0, "deliveries": 0}
    factory = get_session_factory()

    def remaining() -> float:
        return budget - (time.monotonic() - started)

    async def wait_for_due(kind: str, guard: float) -> bool:
        """Sleep a little if more work of this kind falls due in time; False to stop."""
        due = await next_due(kind)
        if due is None or due > remaining() - guard:
            return False
        await asyncio.sleep(min(max(due, 0.2), 5.0))
        return True

    async def draft_worker(client: httpx.AsyncClient) -> None:
        guard = _draft_guard(settings)
        while remaining() > guard:
            async with factory() as session:
                job = await drafting.claim_job(session, settings.job_lease_seconds)
            if job is None:
                if await wait_for_due("drafts", guard):
                    continue
                return
            await drafting.process_job(factory, client, settings, job)
            stats["drafts"] += 1

    async def delivery_worker(client: httpx.AsyncClient) -> None:
        guard = settings.delivery_timeout_seconds + 5
        while remaining() > guard:
            async with factory() as session:
                item = await delivery.claim_delivery(session, settings.delivery_lease_seconds)
            if item is None:
                if await wait_for_due("deliveries", guard):
                    continue
                return
            await delivery.process_delivery(factory, client, settings, item)
            stats["deliveries"] += 1

    async def run_all(http: httpx.AsyncClient) -> None:
        await asyncio.gather(
            *(draft_worker(http) for _ in range(settings.worker_concurrency)),
            *(delivery_worker(http) for _ in range(settings.delivery_concurrency)),
        )

    if client is not None:
        await run_all(client)
    else:
        async with httpx.AsyncClient() as http:
            await run_all(http)
    stats["seconds"] = round(time.monotonic() - started, 2)
    pending = [k for k in _NEXT_DUE if await next_due(k) is not None]
    stats["pending"] = pending
    if pending and base_url:
        stats["next_tick"] = await kick(base_url, settings)
    log.info("tick done: %s", stats)
    return stats


async def kick(base_url: str, settings: Settings) -> bool:
    """Start a tick in a separate invocation, at most once per kick interval.

    Runs as a background task after the webhook or approval response was sent,
    so the caller never waits for drafting. Returns True when a tick was started.
    """
    if not settings.serverless:
        return False  # the worker container polls the queue itself
    try:
        async with get_session_factory()() as session:
            due = (await session.execute(
                _KICK, {"interval": settings.kick_interval_seconds})).first()
            await session.commit()
        if due is None:
            return False
        headers = {"authorization": f"Bearer {settings.cron_secret}"} if settings.cron_secret else {}
        async with httpx.AsyncClient() as client:
            # The tick runs for minutes; only make sure the request went out.
            await client.post(f"{base_url.rstrip('/')}/internal/tick", headers=headers,
                              timeout=httpx.Timeout(3.0, read=1.0))
    except httpx.TimeoutException:
        pass
    except Exception:  # noqa: BLE001 - a lost kick is caught up by the scheduled tick
        log.exception("kick failed")
        return False
    return True
