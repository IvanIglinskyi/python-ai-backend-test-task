"""Background worker: `python -m app.workers.runner`.

Runs WORKER_CONCURRENCY draft loops and DELIVERY_CONCURRENCY delivery loops in
one process. All coordination goes through PostgreSQL (SKIP LOCKED claims and
leases), so several worker containers can run side by side.
"""

import asyncio
import logging
import signal

import httpx

from app.config import Settings, get_settings
from app.db.session import get_session_factory, init_models
from app.services import delivery, drafting

log = logging.getLogger("worker")


async def draft_loop(n: int, settings: Settings, client: httpx.AsyncClient,
                     stop: asyncio.Event) -> None:
    factory = get_session_factory()
    while not stop.is_set():
        try:
            async with factory() as session:
                job = await drafting.claim_job(session, settings.job_lease_seconds)
            if job is None:
                await _idle(stop, settings.worker_poll_seconds)
                continue
            log.info("draft-%s took job %s (attempt %s)", n, job.id, job.attempts)
            await drafting.process_job(factory, client, settings, job)
        except Exception:  # noqa: BLE001 - keep the loop alive; the lease brings the job back
            log.exception("draft-%s loop error", n)
            await _idle(stop, 5)


async def delivery_loop(n: int, settings: Settings, client: httpx.AsyncClient,
                        stop: asyncio.Event) -> None:
    factory = get_session_factory()
    while not stop.is_set():
        try:
            async with factory() as session:
                item = await delivery.claim_delivery(session, settings.delivery_lease_seconds)
            if item is None:
                await _idle(stop, settings.worker_poll_seconds)
                continue
            await delivery.process_delivery(factory, client, settings, item)
        except Exception:  # noqa: BLE001
            log.exception("delivery-%s loop error", n)
            await _idle(stop, 5)


async def _idle(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def run_workers(settings: Settings, stop: asyncio.Event) -> None:
    """All worker loops until `stop` is set. Also used by EMBEDDED_WORKER in the API."""
    mode = "live" if settings.llm_api_key else "keyless (static drafts)"
    log.info("worker started: %s draft loops, %s delivery loops, LLM %s",
             settings.worker_concurrency, settings.delivery_concurrency, mode)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        tasks = [
            *(asyncio.create_task(draft_loop(i, settings, client, stop))
              for i in range(settings.worker_concurrency)),
            *(asyncio.create_task(delivery_loop(i, settings, client, stop))
              for i in range(settings.delivery_concurrency)),
        ]
        await asyncio.gather(*tasks)
    log.info("worker stopped")


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    if settings.init_db:
        await init_models()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows
            pass
    await run_workers(settings, stop)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
