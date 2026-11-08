"""PostgreSQL-backed tests for the invariants in SPEC.md."""

import asyncio
import time

import httpx
from sqlalchemy import func, select, text

from app.config import get_settings
from app.db.models import Delivery, Draft, Job, LlmCall, MockReply, Ticket, WebhookEvent
from app.db.session import get_session_factory
from app.main import create_app
from app.services import delivery, drafting, rate_limit

PAYLOAD = {
    "event_id": "evt_8f2a",
    "ticket_id": "T-1042",
    "customer_email": "user@example.com",
    "message": "I can't export my report to PDF",
    "reply_url": "http://helpdesk.test/tickets/T-1042/replies",
}


def _app_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()),
                             base_url="http://service.test")


async def _count(model) -> int:
    async with get_session_factory()() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def _no_sleep(_seconds: float) -> None:
    return None


async def _ingest(payload: dict = PAYLOAD) -> None:
    async with _app_client() as client:
        response = await client.post("/webhooks/helpdesk", json=payload)
        assert response.status_code == 200


async def _draft_ready(llm_handler=None) -> int:
    """Ingest PAYLOAD and run the worker step in keyless mode. Returns the draft id."""
    await _ingest()
    settings = get_settings().model_copy(update={"llm_stub_min_delay": 0, "llm_stub_max_delay": 0})
    handler = llm_handler or (lambda request: httpx.Response(401))
    factory = get_session_factory()
    async with factory() as session:
        job = await drafting.claim_job(session, lease_seconds=300)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await drafting.process_job(factory, client, settings, job)
    async with factory() as session:
        return await session.scalar(select(Draft.id))


def test_concurrent_duplicate_webhooks_store_one_event_and_one_job(db) -> None:
    async def scenario():
        async with _app_client() as client:
            started = time.monotonic()
            responses = await asyncio.gather(
                *(client.post("/webhooks/helpdesk", json=PAYLOAD) for _ in range(25))
            )
            elapsed = time.monotonic() - started
        statuses = sorted(r.json()["status"] for r in responses)
        return [r.status_code for r in responses], statuses, elapsed, \
            await _count(WebhookEvent), await _count(Job)

    codes, statuses, elapsed, events, jobs = db(scenario)
    assert set(codes) == {200}
    assert statuses.count("accepted") == 1
    assert statuses.count("duplicate") == 24
    assert events == 1
    assert jobs == 1
    assert elapsed < 2


def test_burst_of_distinct_events_is_fully_stored(db) -> None:
    async def scenario():
        async with _app_client() as client:
            responses = await asyncio.gather(*(
                client.post("/webhooks/helpdesk",
                            json={**PAYLOAD, "event_id": f"evt_{i}", "ticket_id": f"T-{i}"})
                for i in range(100)
            ))
        return [r.status_code for r in responses], await _count(Job), await _count(Ticket)

    codes, jobs, tickets = db(scenario)
    assert set(codes) == {200}
    assert jobs == 100 and tickets == 100


def test_skip_locked_gives_a_job_to_one_worker(db) -> None:
    async def scenario():
        await _ingest()
        factory = get_session_factory()

        async def claim():
            async with factory() as session:
                return await drafting.claim_job(session, lease_seconds=300)

        return await asyncio.gather(*(claim() for _ in range(10)))

    claims = db(scenario)
    assert sum(c is not None for c in claims) == 1


def test_worker_makes_provider_request_and_stores_static_draft(db) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401)

    async def scenario():
        draft_id = await _draft_ready(handler)
        factory = get_session_factory()
        async with factory() as session:
            draft = await session.get(Draft, draft_id)
            ticket = await session.get(Ticket, "T-1042")
            job = await session.scalar(select(Job))
        return draft, ticket, job, await _count(LlmCall)

    draft, ticket, job, llm_calls = db(scenario)
    assert len(requests) == 1
    assert draft.source == "static"
    assert draft.status == "pending_review"
    assert "export-pdf" in draft.article_ids
    assert "/kb/export-pdf" in draft.body
    assert ticket.status == "pending_review"
    assert job.status == "done"
    assert llm_calls == 1


def test_failed_llm_call_is_retried_later(db, monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    get_settings.cache_clear()

    async def scenario():
        await _ingest()
        factory = get_session_factory()
        async with factory() as session:
            job = await drafting.claim_job(session, lease_seconds=300)
        transport = httpx.MockTransport(lambda request: httpx.Response(500))
        async with httpx.AsyncClient(transport=transport) as client:
            await drafting.process_job(factory, client, get_settings(), job)
        async with factory() as session:
            stored = await session.scalar(select(Job))
            due_now = await session.scalar(text("SELECT available_at <= now() FROM jobs"))
        return stored, due_now, await _count(Draft)

    job, due_now, drafts = db(scenario)
    assert job.status == "queued"
    assert job.attempts == 1
    assert "500" in job.last_error
    assert due_now is False
    assert drafts == 0


def test_rate_limit_is_shared_through_the_database(db) -> None:
    async def scenario():
        factory = get_session_factory()
        waits = []
        for _ in range(4):
            async with factory() as session:
                waits.append(await rate_limit.try_acquire(session, limit=3))
        return waits

    waits = db(scenario)
    assert waits[:3] == [0, 0, 0]
    assert 0 < waits[3] <= 60


def test_concurrent_approvals_create_exactly_one_delivery(db) -> None:
    async def scenario():
        draft_id = await _draft_ready()
        factory = get_session_factory()

        async def approve():
            async with factory() as session:
                try:
                    return await delivery.approve_draft(session, draft_id, "Final reply")
                except delivery.DecisionError:
                    return None

        results = await asyncio.gather(*(approve() for _ in range(10)))
        async with factory() as session:
            stored = await session.scalar(select(Delivery))
        return results, await _count(Delivery), stored

    results, deliveries, stored = db(scenario)
    assert sum(r is not None for r in results) == 1
    assert deliveries == 1
    assert stored.message == "Final reply"
    assert stored.reply_url == PAYLOAD["reply_url"]


def test_rejected_draft_is_never_delivered(db) -> None:
    async def scenario():
        draft_id = await _draft_ready()
        async with get_session_factory()() as session:
            await delivery.reject_draft(session, draft_id)
        async with get_session_factory()() as session:
            claimed = await delivery.claim_delivery(session, lease_seconds=60)
            ticket = await session.get(Ticket, "T-1042")
        return claimed, ticket.status

    claimed, status = db(scenario)
    assert claimed is None
    assert status == "rejected"


def test_delivery_retries_with_same_key_and_sends_once(db) -> None:
    seen: list[httpx.Request] = []
    answers = iter(["503", "timeout", "201"])

    def helpdesk(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = next(answers)
        if answer == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(int(answer))

    async def scenario():
        draft_id = await _draft_ready()
        factory = get_session_factory()
        async with factory() as session:
            await delivery.approve_draft(session, draft_id)
        outcomes = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(helpdesk)) as client:
            for _ in range(5):
                async with factory() as session:
                    # Skip the backoff wait.
                    await session.execute(text("UPDATE deliveries SET available_at = now() "
                                               "WHERE state = 'pending'"))
                    await session.commit()
                    item = await delivery.claim_delivery(session, lease_seconds=60)
                if item is None:
                    break
                outcomes.append((await delivery.process_delivery(
                    factory, client, get_settings(), item)).kind)
        async with factory() as session:
            stored = await session.scalar(select(Delivery))
            ticket = await session.get(Ticket, "T-1042")
        return outcomes, stored, ticket.status

    outcomes, stored, ticket_status = db(scenario)
    assert outcomes == ["retry", "retry", "sent"]
    assert len(seen) == 3
    assert {r.headers["Idempotency-Key"] for r in seen} == {stored.idempotency_key}
    assert stored.state == "sent" and stored.sent_at is not None
    assert ticket_status == "sent"


def test_full_flow_through_mock_helpdesk_delivers_one_reply(db, monkeypatch) -> None:
    """Webhook -> worker -> approve -> mock helpdesk that stores the reply but times out."""
    monkeypatch.setenv("MOCK_SLOW_RATE", "1")
    monkeypatch.setenv("MOCK_SLOW_SECONDS", "0.5")
    get_settings.cache_clear()

    async def scenario():
        factory = get_session_factory()
        async with _app_client() as app_client:
            response = await app_client.post("/mock/helpdesk/tickets", json={
                "event_id": "evt_mock", "ticket_id": "T-7",
                "customer_email": "user@example.com", "message": "How do I invite a member?"})
            assert response.status_code == 200
            async with factory() as session:
                job = await drafting.claim_job(session, lease_seconds=300)
            settings = get_settings().model_copy(
                update={"llm_stub_min_delay": 0, "llm_stub_max_delay": 0,
                        "delivery_timeout_seconds": 0.2})
            llm = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
            async with llm:
                await drafting.process_job(factory, llm, settings, job)
            async with factory() as session:
                draft_id = await session.scalar(select(Draft.id))
                await delivery.approve_draft(session, draft_id)

            for _ in range(6):
                async with factory() as session:
                    await session.execute(text("UPDATE deliveries SET available_at = now() "
                                               "WHERE state = 'pending'"))
                    await session.commit()
                    item = await delivery.claim_delivery(session, lease_seconds=60)
                if item is None:
                    break
                if item.attempts >= 3:  # helpdesk recovers
                    monkeypatch.setenv("MOCK_SLOW_RATE", "0")
                    get_settings.cache_clear()
                await delivery.process_delivery(factory, app_client, settings, item)

        async with factory() as session:
            stored = await session.scalar(select(Delivery))
        return stored, await _count(MockReply)

    stored, replies = db(scenario)
    assert stored.state == "sent"
    assert stored.attempts == 3
    assert replies == 1


def test_serverless_tick_drafts_and_delivers(db) -> None:
    """The Vercel path: one tick drafts every queued ticket, a second one sends the reply."""
    requests: list[httpx.Request] = []

    def outside(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "chat/completions" in request.url.path:
            return httpx.Response(401)
        return httpx.Response(201)

    async def scenario():
        from app.workers.tick import drain

        for i in range(5):
            await _ingest({**PAYLOAD, "event_id": f"evt_t{i}", "ticket_id": f"T-{i}",
                           "reply_url": f"http://helpdesk.test/tickets/T-{i}/replies"})
        settings = get_settings().model_copy(
            update={"llm_stub_min_delay": 0, "llm_stub_max_delay": 0})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outside)) as client:
            first = await drain(settings, budget_seconds=120, client=client)
            async with get_session_factory()() as session:
                draft_id = await session.scalar(select(Draft.id).where(Draft.ticket_id == "T-0"))
                await delivery.approve_draft(session, draft_id)
            second = await drain(settings, budget_seconds=120, client=client)
            third = await drain(settings, budget_seconds=120, client=client)
        async with get_session_factory()() as session:
            states = (await session.scalars(select(Delivery.state))).all()
        return first, second, third, states

    first, second, third, states = db(scenario)
    assert first["drafts"] == 5 and first["deliveries"] == 0
    assert second["deliveries"] == 1
    assert third["drafts"] == 0 and third["deliveries"] == 0
    assert states == ["sent"]
    assert sum("/replies" in r.url.path for r in requests) == 1


def test_tick_does_not_start_a_job_it_cannot_finish(db) -> None:
    async def scenario():
        from app.workers.tick import drain

        await _ingest()
        stats = await drain(get_settings(), budget_seconds=5)
        return stats, await _count(Draft)

    stats, drafts = db(scenario)
    assert stats["drafts"] == 0 and drafts == 0


def test_kicks_are_debounced(db, monkeypatch) -> None:
    monkeypatch.setenv("VERCEL", "1")
    get_settings.cache_clear()

    async def scenario():
        from app.workers import tick

        sent: list[str] = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, **kwargs):
                sent.append(url)

        monkeypatch.setattr(tick.httpx, "AsyncClient", FakeClient)
        settings = get_settings()
        results = [await tick.kick("https://app.test", settings) for _ in range(3)]
        return results, sent

    results, sent = db(scenario)
    assert results == [True, False, False]
    assert sent == ["https://app.test/internal/tick"]


def test_tick_endpoint_requires_cron_secret(db, monkeypatch) -> None:
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    get_settings.cache_clear()

    async def scenario():
        async with _app_client() as client:
            denied = await client.post("/internal/tick")
            allowed = await client.post("/internal/tick",
                                        headers={"authorization": "Bearer s3cret"})
        return denied.status_code, allowed.status_code, allowed.json()

    denied, allowed, body = db(scenario)
    assert denied == 401
    assert allowed == 200
    assert body["drafts"] == 0


def test_tick_picks_up_tickets_that_arrive_while_it_runs(db) -> None:
    async def scenario():
        from app.workers.tick import drain

        await _ingest()
        settings = get_settings().model_copy(
            update={"llm_stub_min_delay": 1.5, "llm_stub_max_delay": 1.5})

        async def late_ticket():
            await asyncio.sleep(0.5)
            await _ingest({**PAYLOAD, "event_id": "evt_late", "ticket_id": "T-late"})

        transport = httpx.MockTransport(lambda request: httpx.Response(401))
        async with httpx.AsyncClient(transport=transport) as client:
            stats, _ = await asyncio.gather(
                drain(settings, budget_seconds=120, client=client), late_ticket())
        return stats

    stats = db(scenario)
    assert stats["drafts"] == 2
    assert stats["pending"] == []


def test_operator_ui_renders_and_htmx_actions_return_the_card(db) -> None:
    async def scenario():
        draft_id = await _draft_ready()
        async with _app_client() as client:
            page = await client.get("/operator")
            review = await client.get("/operator?filter=review")
            stats = await client.get("/operator/stats?filter=all&since=0")
            approved = await client.post(f"/operator/drafts/{draft_id}/approve",
                                         data={"message": "Edited <reply>"},
                                         headers={"HX-Request": "true"})
            again_htmx = await client.post(f"/operator/drafts/{draft_id}/approve",
                                           headers={"HX-Request": "true"})
            again_plain = await client.post(f"/operator/drafts/{draft_id}/approve")
            mock = await client.get("/mock/helpdesk")
        return page, review, stats, approved, again_htmx, again_plain, mock, await _count(Delivery)

    page, review, stats, approved, again_htmx, again_plain, mock, deliveries = db(scenario)
    assert page.status_code == 200 and "T-1042" in page.text and "Approve &amp; send" in page.text
    assert "Needs review" in review.text and "T-1042" in review.text
    assert stats.status_code == 200 and 'id="stats"' in stats.text
    assert approved.status_code == 200
    assert 'id="ticket-T-1042"' in approved.text
    assert "Edited &lt;reply&gt;" in approved.text and "Queued for delivery" in approved.text
    assert again_htmx.status_code == 200 and "Approve &amp; send" not in again_htmx.text
    assert again_plain.status_code == 409
    assert mock.status_code == 200 and "Helpdesk mock" in mock.text
    assert deliveries == 1


def test_ui_rejects_bad_input_without_server_errors(db) -> None:
    async def scenario():
        draft_id = await _draft_ready()
        async with _app_client() as client:
            bad_email = await client.post("/mock/helpdesk/tickets/form", data={
                "ticket_id": "T-9", "customer_email": "not-an-email", "message": "hi"})
            empty_reply = await client.post(f"/operator/drafts/{draft_id}/approve",
                                            data={"message": "   "})
            form_ok = await client.post("/mock/helpdesk/tickets/form", data={
                "ticket_id": "T-9", "customer_email": "a@example.com", "message": "hi"})
        return bad_email.status_code, empty_reply.status_code, form_ok.status_code, \
            await _count(Delivery), await _count(Job)

    bad_email, empty_reply, form_ok, deliveries, jobs = db(scenario)
    assert bad_email == 422
    assert empty_reply == 422 and deliveries == 0
    assert form_ok == 303 and jobs == 2
