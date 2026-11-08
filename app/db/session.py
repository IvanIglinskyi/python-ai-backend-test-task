from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db.base import Base

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_models_ready = False

# Libpq-only URL options that asyncpg does not accept as keyword arguments.
_LIBPQ_ONLY = ("sslmode", "channel_binding")


def _engine_args(raw_url: str, serverless: bool) -> tuple[object, dict]:
    url = make_url(raw_url)
    connect_args: dict = {}
    sslmode = url.query.get("sslmode")
    if sslmode and sslmode != "disable":
        connect_args["ssl"] = "require" if sslmode in ("require", "prefer", "allow") else sslmode
    url = url.difference_update_query(_LIBPQ_ONLY)

    if serverless:
        # Short-lived functions and PgBouncer-style poolers (Neon, Supabase):
        # no client-side pool, no prepared-statement cache.
        connect_args |= {"statement_cache_size": 0, "prepared_statement_cache_size": 0}
        return url, {"poolclass": NullPool, "connect_args": connect_args}
    return url, {
        "pool_pre_ping": True,
        "pool_size": 10,
        "max_overflow": 20,
        # The helpdesk gives up after 2 s; fail fast instead of queueing forever.
        "pool_timeout": 1.5,
        "connect_args": connect_args,
    }


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        url, kwargs = _engine_args(settings.database_url, settings.serverless)
        _engine = create_async_engine(url, **kwargs)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def init_models() -> None:
    """Create missing tables. Safe when several processes start at once."""
    global _models_ready
    if _models_ready:
        return
    async with get_engine().begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(7311001)"))
        await conn.run_sync(Base.metadata.create_all)
    _models_ready = True


async def session_scope() -> AsyncIterator[AsyncSession]:
    # Serverless runtimes may skip the ASGI lifespan; make sure tables exist.
    if get_settings().init_db:
        await init_models()
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def dispose_engine() -> None:
    """Close pooled connections (tests run each case in a fresh event loop)."""
    global _engine, _session_factory, _models_ready
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
    _models_ready = False
