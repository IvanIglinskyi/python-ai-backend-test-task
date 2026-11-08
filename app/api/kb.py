from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.api.operator import templates
from app.kb.search import get_article, load_articles

router = APIRouter(prefix="/kb")


@router.get("", response_class=HTMLResponse)
async def kb_index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "kb.html", {"active": "kb", "articles": load_articles(), "article": None}
    )


@router.get("/{article_id}", response_class=HTMLResponse)
async def kb_article(article_id: str, request: Request) -> HTMLResponse:
    article = get_article(article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="article not found")
    return templates.TemplateResponse(
        request, "kb.html", {"active": "kb", "articles": load_articles(), "article": article}
    )
