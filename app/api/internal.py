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


# GET for Vercel Cron, POST for kicks and external schedulers.
@router.api_route("/tick", methods=["GET", "POST"])
async def tick(request: Request, authorization: str | None = Header(default=None)) -> dict:
    """Drain the draft and delivery queues for up to TICK_SECONDS."""
    _authorize(authorization)
    return await drain(get_settings(), base_url=public_base_url(request))
