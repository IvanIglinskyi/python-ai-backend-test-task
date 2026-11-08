# Specification: Lumenboard support drafts

Stack: FastAPI, PostgreSQL 16, Docker Compose. One process serves HTTP; another (`python -m app.workers.runner`) takes work from the database.

## Invariants

1. `POST /webhooks/helpdesk` answers in under 2 seconds. No LLM call happens in the request.
2. The event is stored before the webhook responds. A redelivery or a concurrent delivery with the same `event_id` yields one `webhook_events` row and one `jobs` row.
3. Tickets are not dropped while the worker is busy. Peak load is up to 500 events in 10 minutes. The queue lives in PostgreSQL.
4. At most 60 LLM calls per minute for the whole service. When the provider answers `429`, all calls stop until `Retry-After` ends (`llm_pause`), and the job goes back to the queue without spending an attempt: provider throttling never turns a ticket into `draft_failed`.
5. Without `LLM_API_KEY` the client still makes the HTTP request to the provider, ignores the response body and, after a 5–30 second delay, stores a static draft. From there the system treats it exactly like a live one.
6. The customer receives an approved reply once. Redelivery after a 5xx or a timeout uses the same `Idempotency-Key`. A `deliveries` row in state `sent` is never sent again.
7. `GET /health` always returns `200` and the text `OK`.

## Helpdesk contract

Inbound, `POST /webhooks/helpdesk`:

```json
{
  "event_id": "evt_8f2a",
  "ticket_id": "T-1042",
  "customer_email": "user@example.com",
  "message": "Не можу експортувати звіт у PDF",
  "reply_url": "https://helpdesk.example.com/tickets/T-1042/replies"
}
```

Reply to the customer, `POST <reply_url>`:

```json
{
  "ticket_id": "T-1042",
  "message": "To export a report to PDF, open..."
}
```

Helpdesk success: `201`. The `Idempotency-Key` header is required. The mock requires it on `POST /mock/helpdesk/tickets/{ticket_id}/replies` and never stores a second row for the same key.

## Knowledge base

Eight English articles in `app/kb/articles.json`, served as a Help Center at `/kb/<id>`; links in drafts are built from `PUBLIC_BASE_URL`. Search is lexical, `app/kb/search.py`: the title and `keywords_uk` weigh double, weak matches are dropped. `keywords_uk` lets Ukrainian tickets find the articles. No separate vector index is needed.

## Statuses

`tickets.status`: `queued`, then `drafting`, `pending_review`, `approved`, `rejected`, `sending`, `sent`, `send_failed`. `draft_failed` — the LLM did not answer within `JOB_MAX_ATTEMPTS` attempts; the operator puts the ticket back into the queue with Retry drafting.

`drafts.status`: `pending_review`, `approved`, `rejected`.

`deliveries.state`: `pending`, `in_flight`, `sent`, `failed`.

`jobs.status`: `queued`, `running`, `done`, `failed`.

## Queue

The worker claims work with `UPDATE jobs ... WHERE id = (SELECT id ... FOR UPDATE SKIP LOCKED LIMIT 1)` for `status IN (queued, running)` and `available_at <= now()`. For `running`, `available_at` is the end of the lease: another worker takes over the job of a crashed one. `attempts` is a fencing token; only the latest owner writes the result. Deliveries are claimed the same way from `deliveries`.

## Serverless (Vercel)

There is no separate worker process. `/internal/tick` (`Authorization: Bearer CRON_SECRET`) runs the same claim-and-process code for up to `TICK_SECONDS`. After responding, the webhook and operator actions start a tick in a separate invocation, at most once per `KICK_INTERVAL_SECONDS` (`worker_kicks`). The webhook still only writes to the database.

## LLM

OpenAI-compatible `chat/completions` (verified with Groq, `qwen/qwen3.8-27b`). The prompt asks for plain text, a fixed sign-off and `Learn more: <url>` lines; the client also strips Markdown and `<think>` blocks because the helpdesk shows raw text. Errors other than `429` are retried with backoff up to `JOB_MAX_ATTEMPTS`.

## Operator UI

`/operator`: counters, filters, ticket cards. Approve (with an editable text), reject, retry delivery, retry drafting. With htmx an action returns the updated card; without JavaScript it redirects with 303 to `/operator`. An empty reply text is refused (`422`).

## Implementation

| Item | Where |
|---|---|
| Webhook intake | `app/services/ingest.py` |
| Claiming jobs, drafting, retries | `app/services/drafting.py`, `app/workers/runner.py` |
| LLM client, keyless mode | `app/services/llm.py` |
| 60/min limit in the database (`llm_calls`, advisory lock) | `app/services/rate_limit.py` |
| Pause after `429`, retry without spending an attempt | `app/services/rate_limit.py` (`llm_pause`), `app/services/drafting.py` |
| Approve, reject, retry delivery | `app/api/operator.py`, `app/services/delivery.py` |
| Sender with `Idempotency-Key = lumenboard-draft-<draft_id>` | `app/services/delivery.py` |
| Flaky mock (`MOCK_FAILURE_RATE`, `MOCK_SLOW_RATE`) | `app/api/mock_helpdesk.py` |
| Help Center and search (`keywords_uk`) | `app/api/kb.py`, `app/kb/` |
| UI (Jinja2, htmx, vendored Tailwind/htmx/Inter) | `app/templates/`, `app/static/vendor/` |
| Tests for concurrent duplicates and single delivery | `tests/test_integration.py` |
| Browser check of every button | `e2e/ui_check.py` |

Send nothing to the customer outside this contract.
