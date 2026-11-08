# Lumenboard support drafts

A backend that drafts customer-support replies with an LLM. The product in the knowledge base is fictional: **Lumenboard**, a dashboards and reporting tool.

1. The helpdesk sends a ticket to `POST /webhooks/helpdesk`. The webhook only stores the event in PostgreSQL and answers in milliseconds.
2. A worker takes the job from a PostgreSQL queue, searches the knowledge base and calls the LLM. LLM calls are limited to 60 per minute for the whole service.
3. An operator sees tickets with their drafts at `/operator`, can edit the text, and approves or rejects it.
4. An approved reply is posted to the helpdesk `reply_url` exactly once. Retries after 5xx or a timeout reuse the same `Idempotency-Key`.

Stack: Python 3.12, FastAPI, SQLAlchemy 2 (async) + asyncpg, PostgreSQL 16, httpx, Jinja2 + Tailwind + htmx for the UI, Docker Compose.

## Live demo (Vercel)

| What | URL |
|---|---|
| Webhook | `POST https://python-ai-backend-test-task.vercel.app/webhooks/helpdesk` |
| Operator inbox | https://python-ai-backend-test-task.vercel.app/operator |
| Helpdesk mock (open tickets, see what customers received) | https://python-ai-backend-test-task.vercel.app/mock/helpdesk |
| Help Center (knowledge base) | https://python-ai-backend-test-task.vercel.app/kb |
| Health | https://python-ai-backend-test-task.vercel.app/health |
| Swagger | https://python-ai-backend-test-task.vercel.app/docs |

The deployment runs in keyless mode with a flaky mock (20% of replies get `503`, 10% are slow), so a delivery sometimes shows 2–3 attempts, yet the customer receives a single reply.

## Quick start

You only need Docker and Docker Compose.

```bash
git clone https://github.com/IvanIglinskyi/python-ai-backend-test-task.git
cd python-ai-backend-test-task
docker compose up --build
```

Three containers start: `db` (PostgreSQL), `api` (FastAPI on port 8000) and `worker`.

| What | Address |
|---|---|
| Operator inbox | http://localhost:8000/operator |
| Helpdesk mock | http://localhost:8000/mock/helpdesk |
| Help Center | http://localhost:8000/kb |
| Health | http://localhost:8000/health → `200 OK` |
| Swagger | http://localhost:8000/docs |
| PostgreSQL | `localhost:5432`, user, password and database: `helpdesk` |

Stop with `docker compose down`; add `-v` to drop the data as well.

### Keyless mode (default)

An API key is optional. Without `LLM_API_KEY` the worker still sends the real HTTP request to the provider (`POST {LLM_BASE_URL}/chat/completions`) and gets `401`. The response body is ignored; after a random 5–30 second pause it stores a static draft built from the matching articles, with links to them. The rest of the system treats it exactly like a live answer. The draft's `source` is `static` instead of `llm`, and the UI shows it.

### With a real model

Any OpenAI-compatible API works (OpenAI, OpenRouter, Groq, a local vLLM, …):

```bash
cp .env.example .env
# set LLM_API_KEY, and LLM_BASE_URL / LLM_MODEL if needed
docker compose up --build
```

Docker Compose substitutes the variables from `.env` into the compose file.

## Trying it out

In the browser:

1. Open http://localhost:8000/mock/helpdesk, pick an example message and click **Send webhook**.
2. The ticket appears in http://localhost:8000/operator. Its card updates itself; the draft shows up after 5–30 seconds.
3. Edit the draft if you like and click **Approve & send**. The mock sometimes answers `503` or after the timeout on purpose, so the card shows the number of attempts.
4. The mock page lists exactly one received reply for that draft.

With curl (bash):

```bash
# A ticket from the helpdesk; repeat the command and you get "duplicate", no second job
curl -s -X POST http://localhost:8000/webhooks/helpdesk \
  -H 'content-type: application/json' \
  -d '{"event_id":"evt_8f2a","ticket_id":"T-1042","customer_email":"user@example.com",
       "message":"I cannot export my report to PDF",
       "reply_url":"http://api:8000/mock/helpdesk/tickets/T-1042/replies"}'

# What the customer received
curl -s http://localhost:8000/mock/helpdesk/tickets/T-1042/replies
```

PowerShell:

```powershell
$body = @{ event_id="evt_8f2a"; ticket_id="T-1042"; customer_email="user@example.com";
  message="I cannot export my report to PDF";
  reply_url="http://api:8000/mock/helpdesk/tickets/T-1042/replies" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8000/webhooks/helpdesk `
  -ContentType "application/json" -Body $body
```

`reply_url` must be reachable from the `worker` container, so manual webhooks use `http://api:8000/...` rather than `localhost`. Tickets opened through `/mock/helpdesk` get the right address automatically (`MOCK_REPLY_BASE_URL=http://api:8000`).

## Tests

Integration tests run against a real PostgreSQL in a separate `helpdesk_test` database created by `docker/initdb/01-test-db.sql`. GitHub Actions runs the same suite on every push (`.github/workflows/tests.yml`).

```bash
docker compose run --rm tests
```

What is covered:

- 25 concurrent deliveries of one event give one `webhook_events` row, one `jobs` row, and all `200` responses within 2 seconds;
- 100 distinct events at once are all stored;
- `SKIP LOCKED`: out of 10 competing workers only one gets the job;
- in keyless mode the worker calls the provider and stores a static draft that links to the article;
- an LLM error puts the job back with backoff;
- the LLM rate limit is shared through the database;
- 10 concurrent approvals of one draft create one delivery;
- a delivery retried after `503` and a timeout keeps the same `Idempotency-Key` and stops after `201`;
- the full path through a mock that stores the reply but answers too late: the customer gets exactly one reply;
- the serverless tick: drafts, deliveries, tickets arriving mid-tick, kick debouncing, the cron secret;
- the operator UI and htmx actions, the Help Center pages, knowledge-base search, the LLM client modes.

Without Docker, against a local PostgreSQL:

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5432/helpdesk_test python -m pytest
```

Without `TEST_DATABASE_URL` the integration tests are skipped and the unit tests still run.

## Architecture

```
 Helpdesk ──POST /webhooks/helpdesk──▶ api ──INSERT ... ON CONFLICT DO NOTHING──▶ webhook_events, tickets, jobs
                                                                                     │
                       worker: SELECT ... FOR UPDATE SKIP LOCKED ◀───────────────────┘
                         │  search KB → rate limit (llm_calls) → LLM → drafts (pending_review)
                         ▼
 Operator ──/operator──▶ api: approve → drafts.approved + 1 row in deliveries (unique draft_id)
                                                                     │
                       worker: claim delivery (SKIP LOCKED) ◀────────┘
                         │  POST reply_url, Idempotency-Key: lumenboard-draft-<id>
                         ▼
                       201 → sent  │  5xx / timeout → pending + backoff  │  4xx → failed
```

### How the requirements are met

**The webhook answers within 2 seconds.** The handler runs three `INSERT`s in one transaction and nothing else; no LLM call happens in the request. The connection pool has `pool_timeout=1.5s`: if the database is overloaded it is better to fail fast with a 5xx so the helpdesk retries than to hang.

**Retries and concurrent duplicates.** `webhook_events.event_id` is the primary key and the insert uses `INSERT ... ON CONFLICT DO NOTHING RETURNING`. When two identical events arrive at once, the second transaction waits for the first, hits the conflict and answers `200 {"status":"duplicate"}`. The `jobs` row is created in the same transaction with a unique `event_id`. The event is stored before the response, so a `200` means the ticket is safely in the database.

**Peaks of 500 tickets in 10 minutes, nothing lost.** The queue lives in PostgreSQL. The worker runs `WORKER_CONCURRENCY=20` loops in parallel because one LLM call takes 5–30 seconds. A job is claimed with `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`, which also sets a lease (`available_at = now() + 5 min`). If a worker dies, another one takes the job when the lease expires. `attempts` acts as a fencing token, so only the latest owner stores a result. Several worker containers can run side by side: `docker compose up --scale worker=3`.

**60 LLM calls per minute for the whole service.** Every call writes a row to `llm_calls`. Before calling, the worker takes `pg_advisory_xact_lock` and counts the calls of the last 60 seconds; at 60 it waits until the window frees a slot. The limit is shared by all replicas. A peak of 500 tickets in 10 minutes is 50 per minute, below the limit; anything above simply waits in the queue.

**LLM errors.** Exponential backoff from 10 seconds to 10 minutes. After `JOB_MAX_ATTEMPTS=5` the ticket becomes `draft_failed` and stays visible to the operator.

**The customer never gets the same reply twice.**
- Approval is a single `UPDATE drafts ... WHERE status='pending_review' RETURNING`, so a second click or a concurrent request changes nothing (`409`). `deliveries.draft_id` is unique too.
- The reply text is frozen in `deliveries.message`; retries always send the same text.
- `Idempotency-Key = lumenboard-draft-<draft_id>` is deterministic and identical across attempts.
- `5xx`, `408`, `429` and timeouts retry the same delivery with backoff. After a timeout the helpdesk may already have stored the reply, which is exactly why the key stays the same. `201` (and `200`, or `409` "key already used") moves the delivery to `sent`, and nobody claims it again. Other `4xx` or exhausting `DELIVERY_MAX_ATTEMPTS` end in `send_failed`; the operator can click **Retry delivery**, still with the same key.
- The mock enforces the `Idempotency-Key` with a unique index and never stores a second row for the same key.

**Helpdesk mock.** `MOCK_FAILURE_RATE` (default 0.2) is the share of `503` answers. `MOCK_SLOW_RATE` (0.1) is the share answered after 15 seconds, longer than the 10-second client timeout. Half of the slow answers store the reply **before** the pause — the nastiest case for duplicates.

**Knowledge base.** Eight short AI-generated articles live in `app/kb/articles.json` and are served by the app as a Help Center at `/kb/<id>`, which is where draft links point. Search is lexical — token and word-prefix overlap with titles weighted double, weak matches dropped (`app/kb/search.py`). For eight articles a vector index would add dependencies and nothing else; with hundreds of articles the next step would be `pgvector` in the same database.

### Statuses

- `tickets.status`: `queued` → `drafting` → `pending_review` → `approved` / `rejected` → `sending` → `sent` / `send_failed`; `draft_failed` if the LLM never answered.
- `drafts.status`: `pending_review`, `approved`, `rejected`.
- `deliveries.state`: `pending`, `in_flight`, `sent`, `failed`.

### Layout

```
app/
  main.py                 FastAPI app, /health, optional embedded worker (EMBEDDED_WORKER=1)
  config.py               all settings (env)
  api/webhooks.py         POST /webhooks/helpdesk
  api/operator.py         /operator: inbox, approve, reject, retry delivery (htmx)
  api/mock_helpdesk.py    helpdesk mock
  api/kb.py               Help Center pages /kb
  api/internal.py         /internal/tick for serverless
  services/ingest.py      idempotent event intake
  services/drafting.py    claiming and processing draft jobs
  services/llm.py         LLM client (live and keyless)
  services/rate_limit.py  60/min limit in PostgreSQL
  services/delivery.py    operator decisions and sending replies
  workers/runner.py       worker process
  workers/tick.py         worker as a time-boxed function (Vercel)
  kb/                     articles and search
  templates/              Jinja2 templates (Tailwind + htmx)
tests/                    unit and integration tests
docker/initdb/            test database bootstrap
api/index.py, vercel.json Vercel entrypoint
.github/workflows/        CI with PostgreSQL, backup tick schedule
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://helpdesk:helpdesk@db:5432/helpdesk` | `postgres://` is accepted too |
| `INIT_DB` | `1` | create tables on startup |
| `PUBLIC_BASE_URL` | `http://localhost:8000` (on Vercel: the production URL) | base for Help Center links in drafts |
| `LLM_API_KEY` | empty | empty means keyless mode with static drafts |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | any OpenAI-compatible API |
| `LLM_MODEL` | `gpt-4o-mini` | |
| `LLM_RATE_LIMIT_PER_MINUTE` | `60` | shared limit |
| `WORKER_CONCURRENCY` | `20` | parallel drafts per worker |
| `DELIVERY_CONCURRENCY` | `4` | parallel deliveries |
| `JOB_MAX_ATTEMPTS` | `5` | draft attempts |
| `DELIVERY_TIMEOUT_SECONDS` | `10` | timeout of one POST to `reply_url` |
| `DELIVERY_MAX_ATTEMPTS` | `8` | delivery attempts |
| `EMBEDDED_WORKER` | `0` | run the worker inside the API process (single-container hosting) |
| `CRON_SECRET` | empty | protects `/internal/tick` (required on a public deployment) |
| `TICK_SECONDS` | `240` | how long one serverless tick runs |
| `KICK_INTERVAL_SECONDS` | `10` | at most one triggered tick per interval |
| `MOCK_FAILURE_RATE` / `MOCK_SLOW_RATE` | `0.2` / `0.1` | mock flakiness |
| `MOCK_REPLY_BASE_URL` | request URL | how the worker reaches the mock; `http://api:8000` in compose |

## Deployment

### Vercel (primary)

Vercel has no long-running process, so the worker runs as a time-boxed function:

- `/internal/tick` drains the draft and delivery queues for up to `TICK_SECONDS` (240 s; function `maxDuration` is 300 s) and only starts work it can finish. PostgreSQL leases make overlapping ticks safe, and work interrupted midway is picked up by the next tick.
- While a tick runs, its idle workers pick up new tickets every couple of seconds. It waits for retries that fall due within its budget, and if work is still pending when it ends, it starts the next tick.
- The webhook, the mock, approval and delivery retry start a tick in a separate invocation via `BackgroundTasks` after the response is sent, at most once per `KICK_INTERVAL_SECONDS` (`worker_kicks` table), so a 500-ticket peak does not multiply functions. The webhook itself still only writes to the database.
- Safety nets: Vercel Cron once a day (Hobby plan limit) and GitHub Actions `.github/workflows/tick.yml` every 5 minutes.
- The database connection uses no client pool (`NullPool`) and no prepared-statement cache, so it works with the Neon pooler / PgBouncer. `sslmode` and `channel_binding` from the Neon URL are mapped to asyncpg options.

Vercel detects FastAPI on its own (`api/index.py`); no rewrite is needed in `vercel.json` — with one, every request would reach the app as `/api/index`.

Steps:

1. `npm i -g vercel` and `vercel login`.
2. In the repository root: `vercel link`, then `vercel integration add neon` (or Dashboard → Storage → Neon) and connect the database to the project. Vercel adds `DATABASE_URL`.
3. Variables: `vercel env add CRON_SECRET production` (a long random string), optionally `LLM_API_KEY`.
4. `vercel deploy --prod`.
5. For the 5-minute safety net add GitHub secrets `TICK_URL=https://<app>.vercel.app/internal/tick` and `CRON_SECRET`.

Check: `curl https://<app>.vercel.app/health` returns `OK`; open a ticket at `/mock/helpdesk` and watch the draft appear at `/operator`.

### Docker hosting

One image serves both the API and the worker; the port comes from `PORT`.

- **Two services** (Railway, Fly.io, a VPS with `docker compose`): `api` with the default command and `worker` with `python -m app.workers.runner`.
- **One container** (Render, Koyeb): `api` with `EMBEDDED_WORKER=1`. `render.yaml` is included for Render.

## Working with AI agents

The project was built with AI agents, and their configuration is committed:

- `SPEC.md` — the specification and invariants the agent must not break;
- `AGENTS.md`, `CLAUDE.md` — rules for Claude Code and Cursor;
- `helpdesk-builder/` — a separate Agent SDK agent with the tools `webhook_contract` (checks the webhook contract) and `readiness_checklist` (checks readiness against the repository code).

The commit history is kept as is, without squashing.
