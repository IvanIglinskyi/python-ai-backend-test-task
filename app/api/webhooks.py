from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import session_scope
from app.schemas import WebhookIn
from app.services.ingest import accept_event

router = APIRouter()


@router.post("/webhooks/helpdesk")
async def helpdesk_webhook(
    body: WebhookIn,
    session: AsyncSession = Depends(session_scope),
) -> JSONResponse:
    result = await accept_event(session, body.model_dump())
    return JSONResponse(status_code=200, content=result)
