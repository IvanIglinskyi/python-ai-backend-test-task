from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job, Ticket, WebhookEvent


async def accept_event(session: AsyncSession, payload: dict) -> dict:
    """Persist the webhook once. A repeated event_id is a success, not a second job."""
    inserted = await session.execute(
        insert(WebhookEvent)
        .values(
            event_id=payload["event_id"],
            ticket_id=payload["ticket_id"],
            payload=payload,
        )
        .on_conflict_do_nothing(index_elements=[WebhookEvent.event_id])
        .returning(WebhookEvent.event_id)
    )
    new_event_id = inserted.scalar_one_or_none()
    if new_event_id is None:
        await session.commit()
        return {"status": "duplicate", "event_id": payload["event_id"]}

    await session.execute(
        insert(Ticket)
        .values(
            ticket_id=payload["ticket_id"],
            customer_email=payload["customer_email"],
            message=payload["message"],
            reply_url=payload["reply_url"],
            status="queued",
        )
        .on_conflict_do_update(
            index_elements=[Ticket.ticket_id],
            set_={
                "customer_email": payload["customer_email"],
                "message": payload["message"],
                "reply_url": payload["reply_url"],
                "status": "queued",
                "updated_at": func.now(),
            },
        )
    )
    await session.execute(
        insert(Job)
        .values(
            event_id=payload["event_id"],
            kind="generate_draft",
            ticket_id=payload["ticket_id"],
            status="queued",
            attempts=0,
        )
        .on_conflict_do_nothing(index_elements=[Job.event_id])
    )
    await session.commit()
    return {"status": "accepted", "event_id": payload["event_id"]}
