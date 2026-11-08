"""Operator decisions and delivery of approved replies to the helpdesk.

Approval atomically flips the draft from `pending_review` and inserts the single
`deliveries` row for it (unique draft_id). The sender then retries that same row
with the same `Idempotency-Key` until the helpdesk answers 201. A row in `sent`
is never claimed again.
"""

import logging
from dataclasses import dataclass

import httpx
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import Delivery, Draft, Ticket, WebhookEvent

log = logging.getLogger("delivery")


class DecisionError(Exception):
    """The draft is missing or was already decided."""


def idempotency_key_for(draft_id: int) -> str:
    return f"lumenboard-draft-{draft_id}"


async def approve_draft(session: AsyncSession, draft_id: int, message: str | None = None) -> int:
    """Approve a pending draft and enqueue its single delivery. Returns the delivery id."""
    draft = (
        await session.execute(
            update(Draft)
            .where(Draft.id == draft_id, Draft.status == "pending_review")
            .values(status="approved")
            .returning(Draft.id, Draft.ticket_id, Draft.event_id, Draft.body)
        )
    ).first()
    if draft is None:
        await session.rollback()
        raise DecisionError("draft not found or already decided")

    final_message = (message or "").strip() or draft.body
    event = await session.get(WebhookEvent, draft.event_id)
    reply_url = event.payload["reply_url"] if event else (
        await session.scalar(select(Ticket.reply_url).where(Ticket.ticket_id == draft.ticket_id))
    )
    delivery_id = (
        await session.execute(
            insert(Delivery)
            .values(
                draft_id=draft.id,
                ticket_id=draft.ticket_id,
                reply_url=reply_url,
                message=final_message,
                state="pending",
                attempts=0,
                idempotency_key=idempotency_key_for(draft.id),
            )
            .on_conflict_do_nothing(index_elements=[Delivery.draft_id])
            .returning(Delivery.id)
        )
    ).scalar_one_or_none()
    await session.execute(
        update(Ticket).where(Ticket.ticket_id == draft.ticket_id).values(status="approved")
    )
    await session.commit()
    if delivery_id is None:  # unreachable while the draft row guards the transition
        delivery_id = await session.scalar(select(Delivery.id).where(Delivery.draft_id == draft.id))
    return delivery_id


async def reject_draft(session: AsyncSession, draft_id: int) -> None:
    draft = (
        await session.execute(
            update(Draft)
            .where(Draft.id == draft_id, Draft.status == "pending_review")
            .values(status="rejected")
            .returning(Draft.ticket_id)
        )
    ).first()
    if draft is None:
        await session.rollback()
        raise DecisionError("draft not found or already decided")
    await session.execute(
        update(Ticket).where(Ticket.ticket_id == draft.ticket_id).values(status="rejected")
    )
    await session.commit()


async def retry_failed(session: AsyncSession, delivery_id: int) -> None:
    """Re-queue a failed delivery. The idempotency key stays the same."""
    result = await session.execute(
        update(Delivery)
        .where(Delivery.id == delivery_id, Delivery.state == "failed")
        .values(state="pending", attempts=0, available_at=text("now()"), last_error=None)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise DecisionError("delivery not found or not failed")
    await session.commit()


_CLAIM_DELIVERY = text(
    """
    UPDATE deliveries
       SET state = 'in_flight',
           attempts = attempts + 1,
           available_at = now() + make_interval(secs => :lease)
     WHERE id = (
            SELECT id FROM deliveries
             WHERE state IN ('pending', 'in_flight')
               AND available_at <= now()
             ORDER BY available_at, id
             FOR UPDATE SKIP LOCKED
             LIMIT 1)
    RETURNING id, ticket_id, reply_url, message, idempotency_key, attempts
    """
)


@dataclass(frozen=True)
class ClaimedDelivery:
    id: int
    ticket_id: str
    reply_url: str
    message: str
    idempotency_key: str
    attempts: int


async def claim_delivery(session: AsyncSession, lease_seconds: int) -> ClaimedDelivery | None:
    row = (await session.execute(_CLAIM_DELIVERY, {"lease": lease_seconds})).first()
    if row is not None:
        await session.execute(
            update(Ticket).where(Ticket.ticket_id == row.ticket_id).values(status="sending")
        )
    await session.commit()
    if row is None:
        return None
    return ClaimedDelivery(**row._asdict())


@dataclass(frozen=True)
class Outcome:
    kind: str  # "sent" | "retry" | "failed"
    error: str | None = None


async def post_reply(client: httpx.AsyncClient, item: ClaimedDelivery, timeout: float) -> Outcome:
    try:
        response = await client.post(
            item.reply_url,
            json={"ticket_id": item.ticket_id, "message": item.message},
            headers={"Idempotency-Key": item.idempotency_key},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        # Timeout included: the helpdesk may have stored the reply, so retry with the same key.
        return Outcome("retry", f"{exc.__class__.__name__}: {exc}"[:500])
    if response.status_code in (200, 201):
        return Outcome("sent")
    if response.status_code == 409:  # helpdesk says this key was already used
        return Outcome("sent")
    if response.status_code >= 500 or response.status_code in (408, 425, 429):
        return Outcome("retry", f"HTTP {response.status_code}")
    return Outcome("failed", f"HTTP {response.status_code}: {response.text[:300]}")


def backoff_seconds(attempt: int) -> int:
    return min(5 * 2 ** (attempt - 1), 300)


async def process_delivery(
    factory: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    settings: Settings,
    item: ClaimedDelivery,
) -> Outcome:
    outcome = await post_reply(client, item, settings.delivery_timeout_seconds)
    if outcome.kind == "retry" and item.attempts >= settings.delivery_max_attempts:
        outcome = Outcome("failed", f"gave up after {item.attempts} attempts: {outcome.error}")

    params = {"id": item.id, "attempts": item.attempts, "error": outcome.error}
    owned = "WHERE id = :id AND state = 'in_flight' AND attempts = :attempts"
    async with factory() as session:
        if outcome.kind == "sent":
            sql = f"UPDATE deliveries SET state = 'sent', sent_at = now(), last_error = NULL {owned}"
            ticket_status = "sent"
        elif outcome.kind == "retry":
            sql = (
                "UPDATE deliveries SET state = 'pending', last_error = :error, "
                f"available_at = now() + make_interval(secs => :delay) {owned}"
            )
            params["delay"] = backoff_seconds(item.attempts)
            ticket_status = "approved"
        else:
            sql = f"UPDATE deliveries SET state = 'failed', last_error = :error {owned}"
            ticket_status = "send_failed"
        result = await session.execute(text(sql), params)
        if result.rowcount == 1:
            await session.execute(
                update(Ticket).where(Ticket.ticket_id == item.ticket_id).values(status=ticket_status)
            )
        await session.commit()
    log.info("delivery %s attempt %s -> %s %s", item.id, item.attempts, outcome.kind,
             outcome.error or "")
    return outcome
