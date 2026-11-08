"""LLM client: OpenAI-compatible chat completions over httpx.

With LLM_API_KEY the provider answer becomes the draft. Without the key the
client still sends the same HTTP request to the provider (it is rejected with
401), ignores the response body and, after a 5–30 s pause, returns a static
draft built from the knowledge-base articles. Callers cannot tell the two
modes apart except by `source`.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from app.config import Settings, get_settings

log = logging.getLogger("llm")

SYSTEM_PROMPT = (
    "Ти — агент підтримки продукту Lumenboard. Напиши коротку ввічливу чернетку "
    "відповіді клієнту українською мовою. Спирайся лише на надані статті бази знань. "
    "Наприкінці додай посилання на використані статті у форматі «Детальніше: <url>». "
    "Якщо статті не відповідають на питання, чесно скажи, що передаєш звернення "
    "спеціалісту, і не вигадуй функцій продукту."
)


class LLMError(Exception):
    """Provider call failed; the job should be retried later."""


@dataclass(frozen=True)
class DraftResult:
    body: str
    article_ids: list[str]
    source: str  # "llm" or "static"


def build_messages(message: str, articles: list[dict]) -> list[dict]:
    if articles:
        kb = "\n\n".join(
            f"[{a['id']}] {a['title']}\nURL: {a['url']}\n{a['body']}" for a in articles
        )
    else:
        kb = "Релевантних статей не знайдено."
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Звернення клієнта:\n{message}\n\nСтатті бази знань:\n{kb}",
        },
    ]


def static_draft(articles: list[dict]) -> str:
    if not articles:
        return (
            "Вітаємо! Дякуємо за звернення. Ми передали ваше питання спеціалісту "
            "підтримки Lumenboard і відповімо найближчим часом."
        )
    lines = ["Вітаємо! Дякуємо за звернення до підтримки Lumenboard.", ""]
    for article in articles:
        lines.append(f"{article['title']}: {article['body']}")
        lines.append(f"Детальніше: {article['url']}")
        lines.append("")
    lines.append("Якщо це не допомогло, відповідайте на цей лист — ми допоможемо.")
    return "\n".join(lines)


async def generate_draft(
    message: str,
    articles: list[dict],
    *,
    client: httpx.AsyncClient,
    settings: Settings | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> DraftResult:
    """One provider call. The caller is responsible for the rate limit."""
    settings = settings or get_settings()
    article_ids = [a["id"] for a in articles]
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": settings.llm_model,
        "messages": build_messages(message, articles),
        "temperature": 0.2,
    }
    headers = {"content-type": "application/json"}
    if settings.llm_api_key:
        headers["authorization"] = f"Bearer {settings.llm_api_key}"

    if not settings.llm_api_key:
        # Keyless mode: the request still goes to the provider; its answer is ignored.
        try:
            response = await client.post(
                url, json=payload, headers=headers, timeout=settings.llm_timeout_seconds
            )
            log.info("keyless provider call answered %s; using static draft", response.status_code)
        except httpx.HTTPError as exc:
            log.info("keyless provider call failed (%s); using static draft", exc.__class__.__name__)
        await sleep(random.uniform(settings.llm_stub_min_delay, settings.llm_stub_max_delay))
        return DraftResult(body=static_draft(articles), article_ids=article_ids, source="static")

    try:
        response = await client.post(
            url, json=payload, headers=headers, timeout=settings.llm_timeout_seconds
        )
    except httpx.HTTPError as exc:
        raise LLMError(f"provider request failed: {exc.__class__.__name__}") from exc
    if response.status_code != 200:
        raise LLMError(f"provider answered {response.status_code}: {response.text[:200]}")
    try:
        body = response.json()["choices"][0]["message"]["content"].strip()
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise LLMError("unexpected provider response shape") from exc
    if not body:
        raise LLMError("provider returned an empty draft")
    return DraftResult(body=body, article_ids=article_ids, source="llm")
