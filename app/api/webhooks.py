from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import schedule_kick
from app.db.session import session_scope
from app.schemas import WebhookIn
from app.services.ingest import accept_event

router = APIRouter()


@router.post("/webhooks/helpdesk")
async def helpdesk_webhook(
    body: WebhookIn,
    request: Request,
    background: BackgroundTasks,
    session: AsyncSession = Depends(session_scope),
) -> JSONResponse:
    # Only persistence happens before the response; the model is called by the worker.
    result = await accept_event(session, body.model_dump())
    if result["status"] == "accepted":
        schedule_kick(request, background)
    return JSONResponse(status_code=200, content=result, background=background)
