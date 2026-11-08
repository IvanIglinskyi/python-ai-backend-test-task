from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from app.api.mock_helpdesk import router as mock_router
from app.api.operator import router as operator_router
from app.api.webhooks import router as webhook_router
from app.config import get_settings
from app.db.session import init_models


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if get_settings().init_db:
        await init_models()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Lumenboard support drafts", lifespan=lifespan)
    app.include_router(webhook_router)
    app.include_router(operator_router)
    app.include_router(mock_router)

    @app.get("/health", response_class=PlainTextResponse)
    def health() -> str:
        return "OK"

    return app


app = create_app()
