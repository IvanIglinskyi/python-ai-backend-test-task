"""Mock helpdesk: opens tickets (sends our webhook) and accepts replies.

The reply endpoint misbehaves on purpose: MOCK_FAILURE_RATE of requests get 503
without storing anything, MOCK_SLOW_RATE answer only after MOCK_SLOW_SECONDS
(longer than our delivery timeout), half of them after the reply was already
stored. Only the Idempotency-Key keeps the customer from a second copy.
"""

import asyncio
import random
import uuid
from html import escape

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import MockReply
from app.db.session import session_scope
from app.schemas import MockTicketIn, ReplyIn
from app.services.ingest import accept_event

router = APIRouter(prefix="/mock/helpdesk")


def _webhook_payload(request: Request, body: MockTicketIn) -> dict:
    # The worker posts to reply_url, so it must be reachable from the worker, not the browser.
    base = get_settings().mock_reply_base_url or str(request.base_url)
    reply_url = f"{base.rstrip('/')}/mock/helpdesk/tickets/{body.ticket_id}/replies"
    return {
        "event_id": body.event_id or f"evt_{uuid.uuid4().hex[:12]}",
        "ticket_id": body.ticket_id,
        "customer_email": body.customer_email,
        "message": body.message,
        "reply_url": reply_url,
    }


@router.post("/tickets")
async def open_ticket(
    body: MockTicketIn,
    request: Request,
    session: AsyncSession = Depends(session_scope),
) -> dict:
    payload = _webhook_payload(request, body)
    result = await accept_event(session, payload)
    return {**result, "webhook": payload}


@router.get("", response_class=HTMLResponse)
async def mock_home(session: AsyncSession = Depends(session_scope)) -> str:
    replies = (
        await session.scalars(select(MockReply).order_by(MockReply.id.desc()).limit(100))
    ).all()
    rows = "".join(
        f"<tr><td>{escape(r.ticket_id)}</td><td>{escape(r.idempotency_key)}</td>"
        f"<td><pre style='white-space:pre-wrap;margin:0'>{escape(r.message)}</pre></td></tr>"
        for r in replies
    ) or "<tr><td colspan='3'>Відповідей ще немає.</td></tr>"
    return (
        "<!doctype html><html lang='uk'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Мок хелпдеску</title><style>body{font-family:system-ui,sans-serif;"
        "max-width:960px;margin:0 auto;padding:16px}td{border-top:1px solid #ccc;padding:4px;"
        "vertical-align:top}input,textarea{width:100%;box-sizing:border-box}</style></head><body>"
        "<h1>Мок хелпдеску</h1><p><a href='/operator'>До оператора</a></p>"
        "<h2>Нове звернення</h2>"
        "<form method='post' action='/mock/helpdesk/tickets/form'>"
        "<p>Ticket ID <input name='ticket_id' required></p>"
        "<p>Email <input name='customer_email' type='email' value='user@example.com' required></p>"
        "<p>Повідомлення <textarea name='message' required>Не можу експортувати звіт у PDF</textarea></p>"
        "<button type='submit'>Надіслати вебхук</button></form>"
        "<h2>Отримані відповіді (що побачив клієнт)</h2>"
        f"<table><tr><th>Ticket</th><th>Idempotency-Key</th><th>Текст</th></tr>{rows}</table>"
        "</body></html>"
    )


@router.post("/tickets/form")
async def open_ticket_form(
    request: Request,
    ticket_id: str = Form(),
    customer_email: str = Form(),
    message: str = Form(),
    session: AsyncSession = Depends(session_scope),
) -> RedirectResponse:
    body = MockTicketIn(ticket_id=ticket_id, customer_email=customer_email, message=message)
    await accept_event(session, _webhook_payload(request, body))
    return RedirectResponse("/operator", status_code=303)


@router.post("/tickets/{ticket_id}/replies", status_code=201)
async def accept_reply(
    ticket_id: str,
    body: ReplyIn,
    session: AsyncSession = Depends(session_scope),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if body.ticket_id != ticket_id:
        raise HTTPException(status_code=400, detail="ticket_id mismatch")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")

    settings = get_settings()
    roll = random.random()
    if roll < settings.mock_failure_rate:
        return JSONResponse(status_code=503, content={"detail": "mock: temporarily unavailable"})
    slow_phase = None
    if roll < settings.mock_failure_rate + settings.mock_slow_rate:
        slow_phase = random.choice(["before", "after"])
    if slow_phase == "before":
        # Slow before storing: the client times out and nothing was written.
        await asyncio.sleep(settings.mock_slow_seconds)

    inserted = await session.execute(
        insert(MockReply)
        .values(ticket_id=ticket_id, idempotency_key=idempotency_key, message=body.message)
        .on_conflict_do_nothing(index_elements=[MockReply.idempotency_key])
        .returning(MockReply.id)
    )
    created = inserted.scalar_one_or_none() is not None
    await session.commit()
    if slow_phase == "after":
        # Slow after storing: the client times out although the reply is saved.
        await asyncio.sleep(settings.mock_slow_seconds)

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
