import json
import re
from functools import lru_cache
from pathlib import Path

from app.config import get_settings

_ARTICLES_PATH = Path(__file__).with_name("articles.json")
_STOP = {
    "the", "and", "for", "you", "your", "can", "can't", "cannot", "how", "what", "why", "when",
    "with", "from", "this", "that", "into", "are", "was", "not", "but", "does", "doesn't", "don't",
    "have", "has", "our", "out", "all", "any", "get", "use", "want", "need", "please", "help",
    "hello", "thanks", "there", "they", "them", "would", "should", "could", "about", "only",
}
_WORD = re.compile(r"[a-z0-9']+")


@lru_cache
def load_articles() -> tuple[dict, ...]:
    """Articles with absolute URLs served by this app at /kb/<id>."""
    base = get_settings().public_base_url.rstrip("/")
    data = json.loads(_ARTICLES_PATH.read_text(encoding="utf-8"))
    return tuple({**article, "url": f"{base}/kb/{article['id']}"} for article in data)


def get_article(article_id: str) -> dict | None:
    return next((a for a in load_articles() if a["id"] == article_id), None)


def _tokens(text: str) -> set[str]:
    words: set[str] = set()
    for word in _WORD.findall(text.lower().replace("’", "'")):
        if len(word) < 3 or word in _STOP:
            continue
        words.add(word)
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            words.add(word[:-1])  # plans -> plan
        # Crude stemming: "exporting"/"export", "invite"/"invitations" share a prefix.
        if len(word) >= 6:
            words.add(word[:5])
    return words


def search_articles(query: str, limit: int = 3) -> list[dict]:
    """Lexical overlap search; titles count double. Eight articles need no vector index."""
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    scored: list[tuple[int, dict]] = []
    for article in load_articles():
        score = 2 * len(query_tokens & _tokens(article["title"])) + len(
            query_tokens & _tokens(article["body"])
        )
        if score:
            scored.append((score, article))
    if not scored:
        return []
    scored.sort(key=lambda item: item[0], reverse=True)
    # Drop weak matches so a draft does not cite loosely related articles.
    threshold = scored[0][0] / 2
    return [article for score, article in scored[:limit] if score >= threshold]
