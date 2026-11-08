from html import escape

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Draft, Ticket
from app.db.session import session_scope

router = APIRouter()


@router.get("/operator", response_class=HTMLResponse)
async def operator_home(session: AsyncSession = Depends(session_scope)) -> str:
    tickets = (await session.scalars(select(Ticket).order_by(Ticket.created_at.desc()))).all()
    drafts = (await session.scalars(select(Draft))).all()
    drafts_by_ticket: dict[str, list[Draft]] = {}
    for draft in drafts:
        drafts_by_ticket.setdefault(draft.ticket_id, []).append(draft)

    rows = []
    for ticket in tickets:
        ticket_drafts = drafts_by_ticket.get(ticket.ticket_id, [])
        draft_html = "<p>Чернетки ще немає.</p>"
        if ticket_drafts:
            draft_html = "".join(
                f"<p>{escape(item.body)} <small>({escape(item.status)})</small></p>"
                for item in ticket_drafts
            )
        rows.append(
            "<article>"
            f"<h2>{escape(ticket.ticket_id)}</h2>"
            f"<p>{escape(ticket.customer_email)} — {escape(ticket.status)}</p>"
            f"<p>{escape(ticket.message)}</p>"
            f"{draft_html}"
            "</article>"
        )
    body = "".join(rows) or "<p>Звернень ще немає.</p>"
    return (
        "<!doctype html><html lang='uk'><head><meta charset='utf-8'>"
        "<title>Оператор</title></head><body>"
        "<h1>Чернетки підтримки</h1>"
        f"{body}</body></html>"
    )
