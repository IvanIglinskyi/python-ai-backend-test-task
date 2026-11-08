"""Draft generation jobs: claim from PostgreSQL, search the KB, call the LLM, store the draft."""

import logging
from dataclasses import dataclass

import httpx
from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import Draft, Ticket, WebhookEvent
from app.kb.search import search_articles
from app.services import rate_limit
from app.services.llm import LLMError, generate_draft

log = logging.getLogger("drafting")

# Takes a queued job, or a running one whose lease expired (its worker died).
# `attempts` doubles as a fencing token: only the latest claimer may finish the job.
_CLAIM_JOB = text(
    """
    UPDATE jobs
       SET status = 'running',
           attempts = attempts + 1,
           available_at = now() + make_interval(secs => :lease)
     WHERE id = (
            SELECT id FROM jobs
             WHERE kind = 'generate_draft'
               AND status IN ('queued', 'running')
               AND available_at <= now()
             ORDER BY available_at, id
             FOR UPDATE SKIP LOCKED
             LIMIT 1)
    RETURNING id, event_id, ticket_id, attempts
    """
)


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    event_id: str
    ticket_id: str
    attempts: int


async def claim_job(session: AsyncSession, lease_seconds: int) -> ClaimedJob | None:
    row = (await session.execute(_CLAIM_JOB, {"lease": lease_seconds})).first()
    await session.commit()
    if row is None:
        return None
    return ClaimedJob(id=row.id, event_id=row.event_id, ticket_id=row.ticket_id, attempts=row.attempts)


def backoff_seconds(attempt: int) -> int:
    return min(10 * 2 ** (attempt - 1), 600)


async def _finish(session: AsyncSession, job: ClaimedJob, status: str, error: str | None,
                  delay: float = 0, refund: bool = False) -> bool:
    """Close the claim. `refund` gives the attempt back (provider throttling is not a failure)."""
    result = await session.execute(
        text(
            "UPDATE jobs SET status = :status, last_error = :error, "
            "attempts = attempts - :refund, "
            "available_at = now() + make_interval(secs => :delay) "
            "WHERE id = :id AND status = 'running' AND attempts = :attempts"
        ),
        {"status": status, "error": error, "delay": float(delay), "refund": int(refund),
         "id": job.id, "attempts": job.attempts},
    )
    return result.rowcount == 1


async def _requeue_throttled(factory: async_sessionmaker[AsyncSession], job: ClaimedJob,
                             delay: float, error: str) -> None:
    async with factory() as session:
        await _finish(session, job, "queued", error[:1000], delay, refund=True)
        await session.commit()
    log.info("job %s waits %.0fs for the provider rate limit (attempt not counted)", job.id, delay)


async def _fail_or_retry(factory: async_sessionmaker[AsyncSession], settings: Settings,
                         job: ClaimedJob, exc: Exception) -> None:
    """A real failure: retry with backoff, park as draft_failed after the last attempt."""
    error = f"{exc.__class__.__name__}: {exc}"[:1000]
    async with factory() as session:
        if job.attempts >= settings.job_max_attempts:
            log.error("job %s failed for good: %s", job.id, error)
            if await _finish(session, job, "failed", error):
                await session.execute(
                    update(Ticket)
                    .where(Ticket.ticket_id == job.ticket_id, Ticket.status == "drafting")
                    .values(status="draft_failed")
                )
        else:
            delay = backoff_seconds(job.attempts)
            log.warning("job %s attempt %s failed, retry in %ss: %s",
                        job.id, job.attempts, delay, error)
            await _finish(session, job, "queued", error, delay)
        await session.commit()


async def requeue_failed(session: AsyncSession, ticket_id: str) -> int:
    """Operator action: put a ticket whose drafting gave up back into the queue."""
    result = await session.execute(
        text(
            "UPDATE jobs SET status = 'queued', attempts = 0, last_error = NULL, "
            "available_at = now() "
            "WHERE ticket_id = :t AND kind = 'generate_draft' AND status = 'failed'"
        ),
        {"t": ticket_id},
    )
    if result.rowcount:
        await session.execute(
            update(Ticket).where(Ticket.ticket_id == ticket_id).values(status="queued")
        )
    await session.commit()
    return result.rowcount


async def process_job(
    factory: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    settings: Settings,
    job: ClaimedJob,
) -> None:
    async with factory() as session:
        event = await session.get(WebhookEvent, job.event_id)
        await session.execute(
            update(Ticket)
            .where(Ticket.ticket_id == job.ticket_id, Ticket.status == "queued")
            .values(status="drafting")
        )
        await session.commit()
    if event is None:  # cannot happen: the event and the job are written together
        async with factory() as session:
            await _finish(session, job, "failed", "webhook event is missing")
            await session.commit()
        return

    message = event.payload["message"]
    articles = search_articles(message)

    # The provider asked everyone to wait for a while: give the job back instead of
    # holding a worker (or a serverless tick) idle for the whole pause.
    paused = await rate_limit.paused_for(factory)
    if paused > 5:
        await _requeue_throttled(factory, job, paused, "waiting for provider rate limit")
        return

    try:
        await rate_limit.acquire(factory, settings.llm_rate_limit_per_minute)
        result = await generate_draft(message, articles, client=client, settings=settings)
    except LLMError as exc:
        if not exc.throttled:
            await _fail_or_retry(factory, settings, job, exc)
            return
        delay = exc.retry_after if exc.retry_after is not None else backoff_seconds(job.attempts)
        delay = max(delay, 1.0)
        await rate_limit.pause(factory, delay)
        await _requeue_throttled(factory, job, delay, f"LLMError: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - any other failure is retried, then parked
        await _fail_or_retry(factory, settings, job, exc)
        return

    async with factory() as session:
        if not await _finish(session, job, "done", None):
            # Lease expired and another worker took over; let it write the draft.
            await session.rollback()
            log.warning("job %s lost its lease, dropping result", job.id)
            return
        await session.execute(
            insert(Draft)
            .values(
                ticket_id=job.ticket_id,
                event_id=job.event_id,
                body=result.body,
                article_ids=result.article_ids,
                status="pending_review",
                source=result.source,
            )
            .on_conflict_do_nothing(index_elements=[Draft.event_id])
        )
        await session.execute(
            update(Ticket)
            .where(Ticket.ticket_id == job.ticket_id, Ticket.status.in_(["queued", "drafting"]))
            .values(status="pending_review")
        )
        await session.commit()
    log.info("job %s: draft for %s stored (%s)", job.id, job.ticket_id, result.source)
