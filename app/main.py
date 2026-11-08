import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.internal import router as internal_router
from app.api.kb import router as kb_router
from app.api.mock_helpdesk import router as mock_router
from app.api.operator import router as operator_router
from app.api.webhooks import router as webhook_router
from app.config import get_settings
from app.db.session import init_models


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    if settings.init_db:
        await init_models()
    worker_task = None
    stop = asyncio.Event()
    if settings.embedded_worker:
        # Single-container hosting: run the worker loops inside the API process.
        # Handlers never await them, so the webhook stays independent of the LLM.
        from app.workers.runner import run_workers

        logging.basicConfig(level=logging.INFO)
        worker_task = asyncio.create_task(run_workers(settings, stop))
    yield
    if worker_task is not None:
        stop.set()
        await worker_task


def create_app() -> FastAPI:
    app = FastAPI(title="Lumenboard support drafts", lifespan=lifespan)
    app.include_router(webhook_router)
    app.include_router(operator_router)
    app.include_router(mock_router)
    app.include_router(internal_router)
    app.include_router(kb_router)
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).resolve().parent / "static"),
        name="static",
    )

    @app.get("/health", response_class=PlainTextResponse)
    def health() -> str:
        return "OK"

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/operator")

    return app


app = create_app()
