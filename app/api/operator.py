from html import escape

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Delivery, Draft, Ticket, WebhookEvent
from app.db.session import session_scope
from app.kb.search import load_articles
from app.services.delivery import DecisionError, approve_draft, reject_draft, retry_failed

router = APIRouter()

_STYLE = """
body{font-family:system-ui,sans-serif;max-width:960px;margin:0 auto;padding:16px}
article{border:1px solid #ccc;border-radius:6px;padding:12px;margin:12px 0}
.draft{background:#f6f6f6;padding:8px;margin:8px 0;border-radius:4px}
textarea{width:100%;min-height:140px;box-sizing:border-box}
.status{font-family:monospace;padding:1px 6px;border:1px solid #999;border-radius:4px}
pre{white-space:pre-wrap;margin:0}
form.inline{display:inline}
"""


def _articles_html(article_ids: list[str]) -> str:
    by_id = {a["id"]: a for a in load_articles()}
    links = [
        f"<a href='{escape(by_id[i]['url'])}' target='_blank'>{escape(by_id[i]['title'])}</a>"
        for i in article_ids
        if i in by_id
    ]
    return "Статті: " + (", ".join(links) if links else "немає")


def _draft_html(draft: Draft, event_message: str, delivery: Delivery | None) -> str:
    head = (
        f"<p>Чернетка #{draft.id} <span class='status'>{escape(draft.status)}</span> "
        f"<small>джерело: {escape(draft.source)}</small></p>"
        f"<p><b>Повідомлення клієнта:</b> {escape(event_message)}</p>"
        f"<p><small>{_articles_html(draft.article_ids or [])}</small></p>"
    )
    if draft.status == "pending_review":
        body = (
            f"<form method='post' action='/operator/drafts/{draft.id}/approve'>"
            f"<textarea name='message'>{escape(draft.body)}</textarea>"
            "<button type='submit'>Підтвердити й надіслати</button></form>"
            f"<form method='post' action='/operator/drafts/{draft.id}/reject' class='inline'>"
            "<button type='submit'>Відхилити</button></form>"
        )
    else:
        text = delivery.message if delivery else draft.body
        body = f"<pre>{escape(text)}</pre>"
    if delivery:
        body += (
            f"<p>Доставка: <span class='status'>{escape(delivery.state)}</span> "
            f"спроб: {delivery.attempts}"
            + (f", помилка: {escape(delivery.last_error)}" if delivery.last_error else "")
            + "</p>"
        )
        if delivery.state == "failed":
            body += (
                f"<form method='post' action='/operator/deliveries/{delivery.id}/retry'>"
                "<button type='submit'>Повторити доставку</button></form>"
            )
    return f"<div class='draft'>{head}{body}</div>"


@router.get("/operator", response_class=HTMLResponse)
async def operator_home(session: AsyncSession = Depends(session_scope)) -> str:
    tickets = (
        await session.scalars(select(Ticket).order_by(Ticket.updated_at.desc()).limit(200))
    ).all()
    ticket_ids = [t.ticket_id for t in tickets]
    drafts = (
        await session.scalars(
            select(Draft).where(Draft.ticket_id.in_(ticket_ids)).order_by(Draft.id.desc())
        )
    ).all()
    draft_ids = [d.id for d in drafts]
    deliveries = {
        d.draft_id: d
        for d in (
            await session.scalars(select(Delivery).where(Delivery.draft_id.in_(draft_ids)))
        ).all()
    }
    events = {
        e.event_id: e.payload.get("message", "")
        for e in (
            await session.scalars(
                select(WebhookEvent).where(WebhookEvent.event_id.in_([d.event_id for d in drafts]))
            )
        ).all()
    }
    drafts_by_ticket: dict[str, list[Draft]] = {}
    for draft in drafts:
        drafts_by_ticket.setdefault(draft.ticket_id, []).append(draft)

    rows = []
    for ticket in tickets:
        ticket_drafts = drafts_by_ticket.get(ticket.ticket_id, [])
        draft_html = "".join(
            _draft_html(d, events.get(d.event_id, ""), deliveries.get(d.id)) for d in ticket_drafts
        ) or "<p><i>Чернетка готується…</i></p>"
        rows.append(
            "<article>"
            f"<h2>{escape(ticket.ticket_id)} <span class='status'>{escape(ticket.status)}</span></h2>"
            f"<p>{escape(ticket.customer_email)}</p>"
            f"{draft_html}"
            "</article>"
        )
    body = "".join(rows) or "<p>Звернень ще немає.</p>"
    return (
        "<!doctype html><html lang='uk'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Оператор</title><style>{_STYLE}</style></head><body>"
        "<h1>Чернетки підтримки</h1>"
        "<p><a href='/operator'>Оновити</a> · <a href='/mock/helpdesk'>Мок хелпдеску</a></p>"
        f"{body}</body></html>"
    )


def _back() -> RedirectResponse:
    return RedirectResponse("/operator", status_code=303)


@router.post("/operator/drafts/{draft_id}/approve")
async def approve(
    draft_id: int,
    message: str = Form(default=""),
    session: AsyncSession = Depends(session_scope),
) -> RedirectResponse:
    try:
        await approve_draft(session, draft_id, message)
    except DecisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _back()


@router.post("/operator/drafts/{draft_id}/reject")
async def reject(draft_id: int, session: AsyncSession = Depends(session_scope)) -> RedirectResponse:
    try:
        await reject_draft(session, draft_id)
    except DecisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _back()


@router.post("/operator/deliveries/{delivery_id}/retry")
async def retry(delivery_id: int, session: AsyncSession = Depends(session_scope)) -> RedirectResponse:
    try:
        await retry_failed(session, delivery_id)
    except DecisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _back()
