"""Shared fixtures.

Integration tests need PostgreSQL and run only when TEST_DATABASE_URL is set,
e.g. `docker compose run --rm tests`. The schema is recreated for every test.
"""

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import TypeVar

import pytest

from app.config import get_settings

T = TypeVar("T")

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def _quiet_settings(monkeypatch):
    monkeypatch.setenv("INIT_DB", "false")
    monkeypatch.setenv("MOCK_FAILURE_RATE", "0")
    monkeypatch.setenv("MOCK_SLOW_RATE", "0")
    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def run_db(fn: Callable[[], Awaitable[T]]) -> T:
    """Run a coroutine in a fresh loop and close the pool bound to it."""
    from app.db.session import dispose_engine

    async def wrapper() -> T:
        try:
            return await fn()
        finally:
            await dispose_engine()

    return asyncio.run(wrapper())


@pytest.fixture
def db(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()

    from app.db.base import Base
    from app.db.session import get_engine

    async def reset() -> None:
        async with get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    run_db(reset)
    return run_db
