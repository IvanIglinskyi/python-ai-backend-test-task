import json
from functools import lru_cache
from pathlib import Path

_ARTICLES_PATH = Path(__file__).with_name("articles.json")
_STOP = {"щоб", "або", "для", "якщо", "після", "коли", "також", "лише", "немає"}


@lru_cache
def load_articles() -> tuple[dict, ...]:
    data = json.loads(_ARTICLES_PATH.read_text(encoding="utf-8"))
    return tuple(data)


def _tokens(text: str) -> set[str]:
    words = []
    for raw in text.lower().replace("’", "'").split():
        word = "".join(ch for ch in raw if ch.isalnum() or ch == "'")
        if len(word) >= 4 and word not in _STOP:
            words.append(word)
            if len(word) >= 6:
                words.append(word[:5])
    return set(words)


def search_articles(query: str, limit: int = 3) -> list[dict]:
    """Lexical overlap search. Ten articles do not need a vector index."""
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    scored: list[tuple[int, dict]] = []
    for article in load_articles():
        haystack = _tokens(f"{article['title']} {article['body']}")
        score = len(query_tokens & haystack)
        if score:
            scored.append((score, article))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [article for _, article in scored[:limit]]
