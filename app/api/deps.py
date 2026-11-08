from fastapi import BackgroundTasks, Request

from app.config import get_settings
from app.workers.tick import kick


def public_base_url(request: Request) -> str:
    """Base URL as the outside world sees it (behind Vercel or another proxy)."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    if host:
        return f"{proto.split(',')[0].strip()}://{host.split(',')[0].strip()}"
    return str(request.base_url).rstrip("/")


def schedule_kick(request: Request, background: BackgroundTasks) -> None:
    """After the response is sent, start a serverless tick (no-op with a worker container)."""
    settings = get_settings()
    if settings.serverless:
        background.add_task(kick, public_base_url(request), settings)
