"""Operator UI: server-rendered Jinja2 + Tailwind, htmx for in-place actions.

Without JavaScript the forms still work (POST + 303 redirect). With htmx the
same endpoints return the updated ticket card, and cards of tickets that are
still moving (drafting, sending) poll themselves until they settle.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import schedule_kick
from app.db.models import Delivery, Draft, Ticket, WebhookEvent
from app.db.session import session_scope
from app.kb.search import load_articles
from app.services.delivery import DecisionError, approve_draft, reject_draft, retry_failed

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# No trailing punctuation: "see https://x/kb/a." links to https://x/kb/a.
_URL = re.compile(r"https?://[^\s<>\"'&]*[^\s<>\"'&.,;:!?)]")


def linkify(text: str) -> Markup:
    """Escape text and turn bare URLs into links."""
    escaped = str(escape(text or ""))
    return Markup(_URL.sub(
        lambda m: f'<a href="{m.group(0)}" target="_blank" rel="noopener" '
                  f'class="font-medium text-indigo-600 underline decoration-indigo-300 '
                  f'underline-offset-2 hover:text-indigo-500 dark:text-indigo-400">{m.group(0)}</a>',
        escaped,
    ))


templates.env.filters["linkify"] = linkify

# status -> (label, badge classes, dot class)
TICKET_STATUS = {
    "queued": ("Queued", "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300", "bg-slate-400"),
    "drafting": ("Drafting", "bg-sky-50 text-sky-700 dark:bg-sky-950 dark:text-sky-300", "bg-sky-500"),
    "pending_review": ("Needs review", "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300", "bg-amber-500"),
    "approved": ("Approved", "bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300", "bg-indigo-500"),
    "sending": ("Sending", "bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300", "bg-indigo-500"),
    "sent": ("Sent", "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300", "bg-emerald-500"),
    "rejected": ("Rejected", "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400", "bg-slate-400"),
    "send_failed": ("Send failed", "bg-rose-50 text-rose-700 dark:bg-rose-950 dark:text-rose-300", "bg-rose-500"),
    "draft_failed": ("Draft failed", "bg-rose-50 text-rose-700 dark:bg-rose-950 dark:text-rose-300", "bg-rose-500"),
}
LIVE_STATUSES = {"queued", "drafting", "approved", "sending"}

DRAFT_STATUS = {
    "pending_review": ("Awaiting approval", "text-amber-600 dark:text-amber-400"),
    "approved": ("Approved", "text-indigo-600 dark:text-indigo-400"),
    "rejected": ("Rejected", "text-slate-400"),
}

# state -> (label, panel classes, dot class)
DELIVERY_STATE = {
    "pending": ("Queued for delivery", "bg-indigo-50 text-indigo-800 dark:bg-indigo-950 dark:text-indigo-200", "bg-indigo-500"),
    "in_flight": ("Sending to helpdesk", "bg-indigo-50 text-indigo-800 dark:bg-indigo-950 dark:text-indigo-200", "bg-indigo-500"),
    "sent": ("Delivered to customer", "bg-emerald-50 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200", "bg-emerald-500"),
    "failed": ("Delivery failed", "bg-rose-50 text-rose-800 dark:bg-rose-950 dark:text-rose-200", "bg-rose-500"),
}

FILTERS = {
    "all": ("All", None),
    "review": ("Needs review", ["pending_review"]),
    "progress": ("In progress", ["queued", "drafting", "approved", "sending"]),
    "sent": ("Sent", ["sent"]),
    "failed": ("Failed", ["send_failed", "draft_failed"]),
    "rejected": ("Rejected", ["rejected"]),
}


def _ticket_badge(status: str) -> dict:
    label, classes, dot = TICKET_STATUS.get(status, (status, TICKET_STATUS["queued"][1], "bg-slate-400"))
    return {"label": label, "classes": classes, "dot": dot}


def _time(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%b %d, %H:%M UTC") if value else ""


async def _ticket_views(session: AsyncSession, tickets: list[Ticket]) -> list[dict]:
    ticket_ids = [t.ticket_id for t in tickets]
    if not ticket_ids:
        return []
    drafts = (
        await session.scalars(
            select(Draft).where(Draft.ticket_id.in_(ticket_ids)).order_by(Draft.id.desc())
        )
    ).all()
    deliveries = {
        d.draft_id: d
        for d in (
            await session.scalars(select(Delivery).where(Delivery.draft_id.in_([d.id for d in drafts])))
        ).all()
    }
    messages = {
        e.event_id: e.payload.get("message", "")
        for e in (
            await session.scalars(
                select(WebhookEvent).where(WebhookEvent.event_id.in_([d.event_id for d in drafts]))
            )
        ).all()
    }
    articles = {a["id"]: a for a in load_articles()}

    by_ticket: dict[str, list[dict]] = {}
    for d in drafts:
        delivery = deliveries.get(d.id)
        label, text_class = DRAFT_STATUS.get(d.status, (d.status, "text-slate-500"))
        view = {
            "id": d.id,
            "status": d.status,
            "status_badge": {"label": label, "text": text_class},
            "source": d.source,
            "body": d.body,
            "sent_text": delivery.message if delivery else d.body,
            "customer_message": messages.get(d.event_id, ""),
            "articles": [articles[i] for i in (d.article_ids or []) if i in articles],
            "delivery": None,
        }
        if delivery:
            dlabel, panel, dot = DELIVERY_STATE.get(delivery.state, DELIVERY_STATE["pending"])
            view["delivery"] = {
                "id": delivery.id,
                "state": delivery.state,
                "attempts": delivery.attempts,
                "last_error": delivery.last_error if delivery.state != "sent" else None,
                "live": delivery.state in ("pending", "in_flight"),
                "badge": {"label": dlabel, "panel": panel, "dot": dot},
            }
        by_ticket.setdefault(d.ticket_id, []).append(view)

    views = []
    for t in tickets:
        ticket_drafts = by_ticket.get(t.ticket_id, [])
        live = t.status in LIVE_STATUSES or any(
            v["delivery"] and v["delivery"]["live"] for v in ticket_drafts
        )
        views.append({
            "ticket_id": t.ticket_id,
            "key": re.sub(r"[^A-Za-z0-9_-]", "-", t.ticket_id),
            "customer_email": t.customer_email,
            "initials": (t.customer_email[:2] or "?"),
            "message": t.message,
            "status": t.status,
            "status_badge": _ticket_badge(t.status),
            "updated": _time(t.updated_at),
            "drafts": ticket_drafts,
            "live": live,
        })
    return views


async def _stats_context(session: AsyncSession, current: str, since: float) -> dict:
    counts = dict(
        (await session.execute(select(Ticket.status, func.count()).group_by(Ticket.status))).all()
    )
    total = sum(counts.values())

    def total_of(statuses):
        return total if statuses is None else sum(counts.get(s, 0) for s in statuses)

    tabs = [(key, label, total_of(statuses)) for key, (label, statuses) in FILTERS.items()]
    stat_cards = [
        ("review", "Needs review", counts.get("pending_review", 0), "bg-amber-500"),
        ("progress", "In progress", total_of(FILTERS["progress"][1]), "bg-sky-500"),
        ("sent", "Sent", counts.get("sent", 0), "bg-emerald-500"),
        ("failed", "Failed", total_of(FILTERS["failed"][1]), "bg-rose-500"),
    ]
    new_since = 0
    if since:
        new_since = await session.scalar(
            select(func.count()).select_from(Ticket)
            .where(Ticket.created_at > datetime.fromtimestamp(since, UTC))
        ) or 0
    return {"tabs": tabs, "stat_cards": stat_cards, "current": current, "since": since,
            "new_since": new_since}


@router.get("/operator", response_class=HTMLResponse)
async def operator_home(
    request: Request,
    filter: str = "all",
    session: AsyncSession = Depends(session_scope),
) -> HTMLResponse:
    current = filter if filter in FILTERS else "all"
    query = select(Ticket).order_by(Ticket.updated_at.desc()).limit(200)
    statuses = FILTERS[current][1]
    if statuses is not None:
        query = query.where(Ticket.status.in_(statuses))
    tickets = (await session.scalars(query)).all()
    since = datetime.now(UTC).timestamp()
    context = {
        "active": "operator",
        "tickets": await _ticket_views(session, list(tickets)),
        **await _stats_context(session, current, since),
    }
    return templates.TemplateResponse(request, "operator.html", context)


@router.get("/operator/stats", response_class=HTMLResponse)
async def operator_stats(
    request: Request,
    filter: str = "all",
    since: float = 0,
    session: AsyncSession = Depends(session_scope),
) -> HTMLResponse:
    current = filter if filter in FILTERS else "all"
    return templates.TemplateResponse(
        request, "partials/stats.html", await _stats_context(session, current, since)
    )


async def _card(request: Request, session: AsyncSession, ticket_id: str,
                background: BackgroundTasks | None = None) -> HTMLResponse:
    ticket = await session.get(Ticket, ticket_id, populate_existing=True)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    views = await _ticket_views(session, [ticket])
    return templates.TemplateResponse(
        request, "partials/ticket_card.html", {"t": views[0]}, background=background
    )


@router.get("/operator/tickets/{ticket_id}/card", response_class=HTMLResponse)
async def ticket_card(
    ticket_id: str, request: Request, session: AsyncSession = Depends(session_scope)
) -> HTMLResponse:
    return await _card(request, session, ticket_id)


async def _after_action(request: Request, session: AsyncSession, ticket_id: str,
                        background: BackgroundTasks) -> Response:
    """htmx gets the refreshed card; a plain form post is redirected back."""
    if request.headers.get("hx-request"):
        return await _card(request, session, ticket_id, background)
    return RedirectResponse("/operator", status_code=303, background=background)


async def _conflict(request: Request, session: AsyncSession, ticket_id: str | None,
                    exc: DecisionError) -> Response:
    """Already decided elsewhere: htmx shows the current card, plain clients get 409."""
    await session.rollback()
    if request.headers.get("hx-request") and ticket_id:
        return await _card(request, session, ticket_id)
    raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/operator/drafts/{draft_id}/approve")
async def approve(
    draft_id: int,
    request: Request,
    background: BackgroundTasks,
    message: str | None = Form(default=None),
    session: AsyncSession = Depends(session_scope),
) -> Response:
    # Omitted -> send the draft as is; submitted empty -> refuse rather than guess.
    if message is not None and not message.strip():
        raise HTTPException(status_code=422, detail="reply text is empty")
    ticket_id = await session.scalar(select(Draft.ticket_id).where(Draft.id == draft_id))
    try:
        await approve_draft(session, draft_id, message)
    except DecisionError as exc:
        return await _conflict(request, session, ticket_id, exc)
    schedule_kick(request, background)
    return await _after_action(request, session, ticket_id, background)


@router.post("/operator/drafts/{draft_id}/reject")
async def reject(
    draft_id: int,
    request: Request,
    background: BackgroundTasks,
    session: AsyncSession = Depends(session_scope),
) -> Response:
    ticket_id = await session.scalar(select(Draft.ticket_id).where(Draft.id == draft_id))
    try:
        await reject_draft(session, draft_id)
    except DecisionError as exc:
        return await _conflict(request, session, ticket_id, exc)
    return await _after_action(request, session, ticket_id, background)


@router.post("/operator/deliveries/{delivery_id}/retry")
async def retry(
    delivery_id: int,
    request: Request,
    background: BackgroundTasks,
    session: AsyncSession = Depends(session_scope),
) -> Response:
    ticket_id = await session.scalar(select(Delivery.ticket_id).where(Delivery.id == delivery_id))
    try:
        await retry_failed(session, delivery_id)
    except DecisionError as exc:
        return await _conflict(request, session, ticket_id, exc)
    schedule_kick(request, background)
    return await _after_action(request, session, ticket_id, background)
