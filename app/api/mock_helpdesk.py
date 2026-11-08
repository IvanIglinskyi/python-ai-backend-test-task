from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MockReply
from app.db.session import session_scope
from app.schemas import MockTicketIn, ReplyIn
from app.services.ingest import accept_event

router = APIRouter(prefix="/mock/helpdesk")


@router.post("/tickets")
async def open_ticket(
    body: MockTicketIn,
    request: Request,
    session: AsyncSession = Depends(session_scope),
) -> dict:
    reply_url = f"{str(request.base_url).rstrip('/')}/mock/helpdesk/tickets/{body.ticket_id}/replies"
    payload = {
        "event_id": body.event_id,
        "ticket_id": body.ticket_id,
        "customer_email": body.customer_email,
        "message": body.message,
        "reply_url": reply_url,
    }
    result = await accept_event(session, payload)
    return {**result, "webhook": payload}


@router.post("/tickets/{ticket_id}/replies", status_code=201)
async def accept_reply(
    ticket_id: str,
    body: ReplyIn,
    session: AsyncSession = Depends(session_scope),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    if body.ticket_id != ticket_id:
        raise HTTPException(status_code=400, detail="ticket_id mismatch")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")

    inserted = await session.execute(
        insert(MockReply)
        .values(ticket_id=ticket_id, idempotency_key=idempotency_key, message=body.message)
        .on_conflict_do_nothing(index_elements=[MockReply.idempotency_key])
        .returning(MockReply.id)
    )
    created = inserted.scalar_one_or_none() is not None
    await session.commit()
    stored = await session.scalar(select(MockReply).where(MockReply.idempotency_key == idempotency_key))
    return {
        "ticket_id": stored.ticket_id if stored else ticket_id,
        "message": stored.message if stored else body.message,
        "duplicate": not created,
    }


@router.get("/tickets/{ticket_id}/replies")
async def list_replies(
    ticket_id: str,
    session: AsyncSession = Depends(session_scope),
) -> list[dict]:
    rows = (
        await session.scalars(select(MockReply).where(MockReply.ticket_id == ticket_id))
    ).all()
    return [
        {
            "ticket_id": row.ticket_id,
            "message": row.message,
            "idempotency_key": row.idempotency_key,
        }
        for row in rows
    ]
