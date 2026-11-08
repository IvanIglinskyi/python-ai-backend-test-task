from fastapi.testclient import TestClient

from app.config import get_settings
from app.kb.search import search_articles
from app.main import create_app
from app.schemas import WebhookIn


def test_health_returns_ok(monkeypatch) -> None:
    monkeypatch.setenv("INIT_DB", "false")
    get_settings.cache_clear()
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.text == "OK"


def test_webhook_schema_requires_message() -> None:
    payload = {
        "event_id": "evt_8f2a",
        "ticket_id": "T-1042",
        "customer_email": "user@example.com",
        "message": "",
        "reply_url": "https://helpdesk.example.com/tickets/T-1042/replies",
    }
    try:
        WebhookIn.model_validate(payload)
    except Exception:
        return
    raise AssertionError("empty message must be rejected")


def test_search_finds_pdf_article() -> None:
    matches = search_articles("I can't export my report to PDF")
    assert matches
    assert matches[0]["id"] == "export-pdf"
