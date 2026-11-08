from app.kb.search import load_articles


class DraftNotReady(Exception):
    """Raised until the worker calls the provider and stores a draft."""


async def generate_draft(message: str, articles: list[dict] | None = None) -> dict:
    """Next step: real provider call, then a static draft when LLM_API_KEY is empty.

    The call must still hit the provider without a key. The static body replaces
    the provider payload and waits 5–30 seconds. This function does not do that yet.
    """
    del message, articles
    load_articles()
    raise DraftNotReady("draft generation is not implemented")
