"""Tests that need no database."""

import asyncio

import httpx
import pytest

from app.config import Settings
from app.kb.search import search_articles
from app.services.delivery import ClaimedDelivery, post_reply
from app.services.llm import LLMError, generate_draft


async def _no_sleep(_seconds: float) -> None:
    return None


def _settings(**overrides) -> Settings:
    return Settings(database_url="postgresql+asyncpg://unused/unused", **overrides)


def test_keyless_mode_calls_provider_and_returns_static_draft() -> None:
    seen: list[httpx.Request] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401, json={"error": {"message": "no key"}})

    async def record_sleep(seconds: float) -> None:
        delays.append(seconds)

    async def scenario():
        articles = search_articles("Не можу експортувати звіт у PDF")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return articles, await generate_draft(
                "Не можу експортувати звіт у PDF",
                articles,
                client=client,
                settings=_settings(llm_api_key=""),
                sleep=record_sleep,
            )

    articles, result = asyncio.run(scenario())
    assert len(seen) == 1
    assert seen[0].url.path.endswith("/chat/completions")
    assert "authorization" not in seen[0].headers
    assert result.source == "static"
    assert articles[0]["url"] in result.body
    assert result.article_ids[0] == "export-pdf"
    assert len(delays) == 1 and 5 <= delays[0] <= 30


def test_keyless_mode_survives_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await generate_draft("hello", [], client=client,
                                        settings=_settings(llm_api_key=""), sleep=_no_sleep)

    assert asyncio.run(scenario()).source == "static"


def test_live_mode_uses_provider_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(200, json={"choices": [{"message": {"content": " Відповідь "}}]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await generate_draft("hello", [], client=client,
                                        settings=_settings(llm_api_key="sk-test"))

    result = asyncio.run(scenario())
    assert result.source == "llm"
    assert result.body == "Відповідь"


def test_live_mode_raises_on_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await generate_draft("hello", [], client=client, settings=_settings(llm_api_key="k"))

    with pytest.raises(LLMError):
        asyncio.run(scenario())


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(201), "sent"),
        (httpx.Response(409), "sent"),
        (httpx.Response(500), "retry"),
        (httpx.Response(503), "retry"),
        (httpx.Response(429), "retry"),
        (httpx.Response(400), "failed"),
        (httpx.Response(404), "failed"),
        ("timeout", "retry"),
    ],
)
def test_post_reply_classifies_helpdesk_answers(response, expected) -> None:
    item = ClaimedDelivery(id=1, ticket_id="T-1", reply_url="http://helpdesk.test/r",
                           message="hi", idempotency_key="k-1", attempts=1)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Idempotency-Key"] == "k-1"
        if response == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        return response

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await post_reply(client, item, timeout=1)

    assert asyncio.run(scenario()).kind == expected


def test_database_url_options_for_neon() -> None:
    from app.db.session import _engine_args

    url, kwargs = _engine_args(
        "postgresql+asyncpg://u:p@ep-x-pooler.neon.tech/db?sslmode=require&channel_binding=require",
        serverless=True,
    )
    assert "sslmode" not in str(url) and "channel_binding" not in str(url)
    assert kwargs["connect_args"]["ssl"] == "require"
    assert kwargs["connect_args"]["statement_cache_size"] == 0


def test_public_base_url_honours_proxy_headers() -> None:
    from starlette.requests import Request

    from app.api.deps import public_base_url

    scope = {"type": "http", "scheme": "http", "path": "/", "query_string": b"",
             "server": ("internal", 80),
             "headers": [(b"host", b"internal"), (b"x-forwarded-host", b"app.vercel.app"),
                         (b"x-forwarded-proto", b"https")]}
    assert public_base_url(Request(scope)) == "https://app.vercel.app"
