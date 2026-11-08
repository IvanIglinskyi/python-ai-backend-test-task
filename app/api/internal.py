import secrets

from fastapi import APIRouter, Header, HTTPException, Request

from app.api.deps import public_base_url
from app.config import get_settings
from app.workers.tick import drain

router = APIRouter(prefix="/internal")


def _authorize(authorization: str | None) -> None:
    secret = get_settings().cron_secret
    if not secret:
        return  # local runs; set CRON_SECRET on any public deployment
    if not authorization or not secrets.compare_digest(authorization, f"Bearer {secret}"):
        raise HTTPException(status_code=401, detail="invalid cron secret")


async def _tick(request: Request, authorization: str | None) -> dict:
    _authorize(authorization)
    return await drain(get_settings(), base_url=public_base_url(request))


@router.get("/tick", operation_id="tick_cron")
async def tick_cron(request: Request, authorization: str | None = Header(default=None)) -> dict:
    """Vercel Cron: drain the draft and delivery queues for up to TICK_SECONDS."""
    return await _tick(request, authorization)


@router.post("/tick", operation_id="tick")
async def tick(request: Request, authorization: str | None = Header(default=None)) -> dict:
    """Kicks and external schedulers: drain the queues for up to TICK_SECONDS."""
    return await _tick(request, authorization)
