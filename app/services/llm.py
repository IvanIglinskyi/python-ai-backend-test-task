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
    "You are a support agent for Lumenboard, a dashboards and reporting product. "
    "Write a short, friendly draft reply to the customer in English. "
    "Use only the knowledge-base articles provided; do not invent features, prices or steps. "
    "Give concrete steps when the articles contain them. "
    "End with links to the articles you used, one per line, as 'Learn more: <url>'. "
    "If the articles do not answer the question, say that you are passing the ticket "
    "to a specialist and will follow up."
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
        kb = "No relevant articles found."
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Customer message:\n{message}\n\nKnowledge-base articles:\n{kb}",
        },
    ]


def static_draft(articles: list[dict]) -> str:
    if not articles:
        return (
            "Hi there,\n\n"
            "Thanks for reaching out to Lumenboard Support. I've passed your question to a "
            "specialist on our team, and we'll get back to you shortly.\n\n"
            "Best regards,\nLumenboard Support"
        )
    lines = ["Hi there,", "", "Thanks for reaching out to Lumenboard Support. Here's what should help:", ""]
    for article in articles:
        lines.append(f"{article['title']}: {article['body']}")
        lines.append(f"Learn more: {article['url']}")
        lines.append("")
    lines.append("If this doesn't solve it, just reply to this message and we'll help further.")
    lines.append("")
    lines.append("Best regards,\nLumenboard Support")
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
