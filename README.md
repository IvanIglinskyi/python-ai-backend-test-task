# Lumenboard support drafts

Бекенд, який готує чернетки відповідей підтримки за допомогою LLM. Продукт у базі знань вигаданий: **Lumenboard**, сервіс дашбордів і звітів.

1. Хелпдеск надсилає звернення на `POST /webhooks/helpdesk`. Вебхук лише зберігає подію в PostgreSQL і відповідає за мілісекунди.
2. Воркер забирає роботу з черги в PostgreSQL, знаходить статті в базі знань і викликає LLM. Виклики LLM обмежені 60 на хвилину для всього сервісу.
3. Оператор на `/operator` бачить звернення з чернетками. Він може відредагувати текст, а потім підтвердити або відхилити чернетку.
4. Підтверджена відповідь іде на `reply_url` хелпдеску один раз. Повтори після 5xx або таймауту надсилаються з тим самим `Idempotency-Key`.

Стек: Python 3.12, FastAPI, SQLAlchemy 2 (async) + asyncpg, PostgreSQL 16, httpx, Docker Compose.

## Посилання (Vercel)

| Що | URL |
|---|---|
| Вебхук | `POST https://python-ai-backend-test-task.vercel.app/webhooks/helpdesk` |
| Інтерфейс оператора | https://python-ai-backend-test-task.vercel.app/operator |
| Мок хелпдеску (створити звернення, побачити відповіді клієнту) | https://python-ai-backend-test-task.vercel.app/mock/helpdesk |
| Здоров’я | https://python-ai-backend-test-task.vercel.app/health |
| Swagger | https://python-ai-backend-test-task.vercel.app/docs |

На деплої працює режим без API-ключа і нестабільний мок (20 % відповідей `503`, 10 % повільних). Тому біля доставки іноді видно 2–3 спроби, але клієнт отримує одну відповідь.

## Швидкий старт

Потрібні лише Docker і Docker Compose.

```bash
git clone https://github.com/IvanIglinskyi/python-ai-backend-test-task.git
cd python-ai-backend-test-task
docker compose up --build
```

Піднімаються три контейнери: `db` (PostgreSQL), `api` (FastAPI на порту 8000) і `worker`.

| Що | Адреса |
|---|---|
| Інтерфейс оператора | http://localhost:8000/operator |
| Мок хелпдеску: створити звернення і подивитися, що отримав клієнт | http://localhost:8000/mock/helpdesk |
| Здоров’я | http://localhost:8000/health → `200 OK` |
| Swagger | http://localhost:8000/docs |
| PostgreSQL | `localhost:5432`, користувач, пароль і база: `helpdesk` |

Зупинка: `docker compose down`. Щоб також видалити дані: `docker compose down -v`.

### Режим без API-ключа (за замовчуванням)

Ключ не обов’язковий. Без `LLM_API_KEY` воркер однаково робить справжній HTTP-запит до провайдера (`POST {LLM_BASE_URL}/chat/completions`) і отримує `401`. Тіло відповіді ігнорується. Після випадкової паузи 5–30 секунд зберігається статична чернетка, зібрана зі знайдених статей, з посиланнями на них. Решта системи працює з нею так само, як із живою відповіддю. У поле `source` пишеться `static` замість `llm`, і це видно в інтерфейсі.

### З реальною моделлю

Підійде будь-який OpenAI-сумісний API (OpenAI, OpenRouter, Groq, локальний vLLM тощо):

```bash
cp .env.example .env
# у .env вкажіть LLM_API_KEY, а за потреби також LLM_BASE_URL і LLM_MODEL
docker compose up --build
```

Docker Compose сам підставляє змінні з `.env` у файл compose.

## Як перевірити руками

Найпростіше через браузер:

1. Відкрийте http://localhost:8000/mock/helpdesk, введіть Ticket ID і повідомлення, натисніть «Надіслати вебхук».
2. На http://localhost:8000/operator з’явиться звернення. Приблизно через 5–30 секунд з’явиться чернетка (оновіть сторінку).
3. Натисніть «Підтвердити й надіслати». Мок навмисно іноді відповідає `503` або довше за таймаут, тому біля доставки видно кількість спроб.
4. На сторінці мока в таблиці «Отримані відповіді» буде рівно один рядок для цієї чернетки.

Через curl (bash):

```bash
# Звернення від хелпдеску; повторіть команду — буде "duplicate", друга робота не створиться
curl -s -X POST http://localhost:8000/webhooks/helpdesk \
  -H 'content-type: application/json' \
  -d '{"event_id":"evt_8f2a","ticket_id":"T-1042","customer_email":"user@example.com",
       "message":"Не можу експортувати звіт у PDF",
       "reply_url":"http://localhost:8000/mock/helpdesk/tickets/T-1042/replies"}'

# Що отримав клієнт
curl -s http://localhost:8000/mock/helpdesk/tickets/T-1042/replies
```

PowerShell:

```powershell
$body = @{ event_id="evt_8f2a"; ticket_id="T-1042"; customer_email="user@example.com";
  message="Не можу експортувати звіт у PDF";
  reply_url="http://localhost:8000/mock/helpdesk/tickets/T-1042/replies" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8000/webhooks/helpdesk `
  -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

`reply_url` має вказувати на адресу, доступну з контейнера `worker`. Мок сам підставляє правильну адресу (`MOCK_REPLY_BASE_URL=http://api:8000`), коли звернення створюється через `/mock/helpdesk`. Якщо надсилаєте вебхук вручну, `localhost:8000` у `reply_url` всередині контейнера не відповість, тому вкажіть `http://api:8000/mock/helpdesk/tickets/T-1042/replies`.

## Тести

Інтеграційні тести працюють зі справжнім PostgreSQL в окремій базі `helpdesk_test`. Її створює `docker/initdb/01-test-db.sql`.

```bash
docker compose run --rm tests
```

Що перевіряється:

- 25 паралельних доставок однієї події дають один рядок `webhook_events`, одну роботу `jobs` і всі відповіді `200` швидше ніж за 2 секунди;
- 100 різних подій одночасно: усі збережені;
- `SKIP LOCKED`: із 10 конкурентних воркерів одну роботу отримує лише один;
- у режимі без ключа воркер робить запит до провайдера і зберігає статичну чернетку з посиланням на статтю;
- помилка LLM переводить роботу на повтор із backoff;
- ліміт викликів LLM спільний через базу;
- 10 паралельних підтверджень однієї чернетки створюють одну доставку;
- доставка після `503` і таймауту повторюється з тим самим `Idempotency-Key` і зупиняється після `201`;
- повний шлях через мок, який зберігає відповідь, але не встигає відповісти: клієнт отримує рівно одну відповідь;
- класифікація відповідей хелпдеску, режими LLM-клієнта, пошук у базі знань.

Без Docker, якщо PostgreSQL запущено локально:

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
TEST_DATABASE_URL=postgresql+asyncpg://helpdesk:helpdesk@localhost:5432/helpdesk_test python -m pytest
```

Без `TEST_DATABASE_URL` інтеграційні тести пропускаються, а юніт-тести виконуються.

## Архітектура

```
 Helpdesk ──POST /webhooks/helpdesk──▶ api ──INSERT ... ON CONFLICT DO NOTHING──▶ webhook_events, tickets, jobs
                                                                                     │
                       worker: SELECT ... FOR UPDATE SKIP LOCKED ◀───────────────────┘
                         │  search KB → rate limit (llm_calls) → LLM → drafts (pending_review)
                         ▼
 Operator ──/operator──▶ api: approve → drafts.approved + 1 row deliveries (unique draft_id)
                                                                     │
                       worker: claim delivery (SKIP LOCKED) ◀────────┘
                         │  POST reply_url, Idempotency-Key: lumenboard-draft-<id>
                         ▼
                       201 → sent  │  5xx / timeout → pending + backoff  │  4xx → failed
```

### Як виконано вимоги

**Вебхук відповідає швидше ніж за 2 секунди.** Обробник робить три `INSERT` в одній транзакції і більше нічого. LLM у HTTP-запиті не викликається. Пул з’єднань має `pool_timeout=1.5s`: якщо база перевантажена, краще швидко віддати 5xx, щоб хелпдеск повторив доставку, ніж мовчати.

**Повтори й паралельні дублікати.** `webhook_events.event_id` є первинним ключем, запис іде через `INSERT ... ON CONFLICT DO NOTHING RETURNING`. Коли дві однакові події приходять одночасно, друга транзакція чекає на першу, отримує конфлікт і відповідає `200 {"status":"duplicate"}`. Робота `jobs` створюється в тій самій транзакції й має унікальний `event_id`. Подія зберігається до відповіді, тож `200` означає, що звернення вже в базі.

**Піки до 500 звернень за 10 хвилин, нічого не губиться.** Черга живе в PostgreSQL. Воркер запускає `WORKER_CONCURRENCY=20` паралельних циклів, бо один виклик LLM триває 5–30 секунд. Роботу забирає `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)` і одночасно ставить лізинг (`available_at = now() + 5 хв`). Якщо воркер упав, після закінчення лізингу роботу забере інший. Лічильник `attempts` працює як fencing token: результат зберігає лише останній власник роботи. Можна запускати кілька контейнерів воркера: `docker compose up --scale worker=3`.

**60 запитів до LLM на хвилину на весь сервіс.** Кожен виклик пише рядок у `llm_calls`. Перед викликом воркер бере `pg_advisory_xact_lock` і рахує виклики за останні 60 секунд. Якщо їх 60, воркер чекає, доки звільниться місце у вікні. Ліміт спільний для всіх реплік. Пік у 500 звернень за 10 хвилин — це 50 на хвилину, тобто менше за ліміт. Якщо буде більше, робота просто почекає в черзі.

**Помилки LLM.** Повтор із експоненційним backoff, від 10 секунд до 10 хвилин. Після `JOB_MAX_ATTEMPTS=5` спроб звернення отримує статус `draft_failed` і лишається в списку оператора.

**Клієнт не отримує ту саму відповідь двічі.**
- Підтвердження виконується одним `UPDATE drafts ... WHERE status='pending_review' RETURNING`, тому друге натискання або паралельний запит нічого не змінить (`409`). `deliveries.draft_id` теж унікальний.
- Текст відповіді фіксується в `deliveries.message`. Повтори завжди надсилають той самий текст.
- `Idempotency-Key = lumenboard-draft-<draft_id>` детермінований і не змінюється між спробами.
- `5xx`, `408`, `429` і таймаут означають повтор тієї самої доставки з backoff. Після таймауту хелпдеск міг уже зберегти відповідь, і саме тому ключ той самий. `201` (а також `200` і `409` «ключ уже використано») переводить доставку в `sent`, і її більше ніхто не забере. Решта `4xx` або вичерпані `DELIVERY_MAX_ATTEMPTS` переводять доставку в `send_failed`. Оператор може натиснути «Повторити доставку» з тим самим ключем.
- Мок перевіряє `Idempotency-Key` (унікальний індекс) і не створює другий рядок для того самого ключа.

**Мок хелпдеску.** `MOCK_FAILURE_RATE` (за замовчуванням 0.2) — частка відповідей `503`. `MOCK_SLOW_RATE` (0.1) — частка відповідей через 15 секунд, що довше за таймаут клієнта в 10 секунд. Половина повільних відповідей зберігає репліку **до** паузи. Це найнеприємніший випадок для дублікатів.

**База знань.** Вісім коротких статей згенеровано AI, вони лежать у `app/kb/articles.json`. Пошук лексичний, за перетином токенів і префіксів слів (`app/kb/search.py`). Для восьми статей векторний індекс не дав би нічого, крім залежностей. Якщо статей стане сотні, варто перейти на `pgvector` у тій самій базі.

### Статуси

- `tickets.status`: `queued` → `drafting` → `pending_review` → `approved` / `rejected` → `sending` → `sent` / `send_failed`; `draft_failed`, якщо LLM не відповів після всіх спроб.
- `drafts.status`: `pending_review`, `approved`, `rejected`.
- `deliveries.state`: `pending`, `in_flight`, `sent`, `failed`.

### Структура

```
app/
  main.py               FastAPI, /health, вбудований воркер за EMBEDDED_WORKER=1
  config.py             усі налаштування (env)
  api/webhooks.py       POST /webhooks/helpdesk
  api/operator.py       /operator: список, підтвердити, відхилити, повторити доставку
  api/mock_helpdesk.py  мок хелпдеску
  api/internal.py       /internal/tick для serverless
  services/ingest.py    ідемпотентний прийом події
  services/drafting.py  забирання й обробка робіт генерації
  services/llm.py       LLM-клієнт (живий і без ключа)
  services/rate_limit.py  ліміт 60/хв у PostgreSQL
  services/delivery.py  рішення оператора й надсилання відповіді
  workers/runner.py     процес воркера
  workers/tick.py       воркер як обмежена в часі функція (Vercel)
  kb/                   статті й пошук
tests/                  юніт- та інтеграційні тести
docker/initdb/          створення тестової бази
api/index.py, vercel.json  точка входу Vercel
.github/workflows/      CI з PostgreSQL і запасний tick
```

## Змінні оточення

| Змінна | За замовчуванням | Опис |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://helpdesk:helpdesk@db:5432/helpdesk` | `postgres://` теж приймається |
| `INIT_DB` | `1` | створити таблиці під час старту |
| `LLM_API_KEY` | порожньо | без ключа працює режим зі статичною чернеткою |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | будь-який OpenAI-сумісний API |
| `LLM_MODEL` | `gpt-4o-mini` | |
| `LLM_RATE_LIMIT_PER_MINUTE` | `60` | спільний ліміт |
| `WORKER_CONCURRENCY` | `20` | паралельні генерації в одному воркері |
| `DELIVERY_CONCURRENCY` | `4` | паралельні доставки |
| `JOB_MAX_ATTEMPTS` | `5` | спроби генерації |
| `DELIVERY_TIMEOUT_SECONDS` | `10` | таймаут одного POST на `reply_url` |
| `DELIVERY_MAX_ATTEMPTS` | `8` | спроби доставки |
| `EMBEDDED_WORKER` | `0` | запускати воркер у процесі API (для хостингу з одним контейнером) |
| `MOCK_FAILURE_RATE` / `MOCK_SLOW_RATE` | `0.2` / `0.1` | нестабільність мока |
| `CRON_SECRET` | порожньо | захищає `/internal/tick` (обов’язково на публічному деплої) |
| `TICK_SECONDS` | `240` | скільки триває один serverless-tick |
| `KICK_INTERVAL_SECONDS` | `10` | не частіше одного запуску tick за цей час |
| `MOCK_REPLY_BASE_URL` | адреса запиту | звідки воркер бачить мок; у compose `http://api:8000` |

## Деплой

### Vercel (основний)

На Vercel немає постійно запущеного процесу, тому воркер працює як обмежена в часі функція:

- `/internal/tick` обробляє черги чернеток і доставок до `TICK_SECONDS` (240 с, `maxDuration` функції 300 с). Нову роботу функція бере, лише якщо встигне її завершити. Лізинги в PostgreSQL роблять одночасні виклики `tick` безпечними, а роботу, перервану на середині, підхопить наступний виклик.
- Поки `tick` працює, його вільні воркери щосекунди підхоплюють нові звернення. Повтори, що настають у межах бюджету, `tick` дочікується сам. Якщо після завершення ще лишилася робота, він запускає наступний `tick`.
- Вебхук, створення звернення через мок, підтвердження і повтор доставки після відповіді клієнту запускають `tick` окремим викликом через `BackgroundTasks`. Запуск відбувається не частіше ніж раз на `KICK_INTERVAL_SECONDS` (таблиця `worker_kicks`), тож у пік у 500 звернень функції не множаться. Сам вебхук, як і раніше, лише пише в базу.
- Запасний запуск: Vercel Cron раз на добу (ліміт плану Hobby) і GitHub Actions `.github/workflows/tick.yml` кожні 5 хвилин. Вони підхоплюють повтори з backoff і втрачені запуски.
- Підключення до бази без пулу (`NullPool`) і без кешу prepared statements, тож працює з Neon pooler / PgBouncer. Параметри `sslmode` і `channel_binding` з URL Neon перетворюються на параметри asyncpg.

Кроки:

Vercel сам розпізнає FastAPI (`api/index.py`). Rewrite у `vercel.json` не потрібен: з ним усі запити приходили б у застосунок як `/api/index`.

1. `npm i -g vercel` і `vercel login`.
2. У корені репозиторію: `vercel link`, потім `vercel integration add neon` (або Dashboard → Storage → Neon) і підключити базу до проєкту. Vercel сам додасть `DATABASE_URL`.
3. Змінні: `vercel env add CRON_SECRET production` (довгий випадковий рядок), за бажанням `LLM_API_KEY`.
4. `vercel deploy --prod`.
5. Для запасного розкладу додайте в GitHub секрети `TICK_URL=https://<app>.vercel.app/internal/tick` і `CRON_SECRET`.

Перевірка: `curl https://<app>.vercel.app/health` повертає `OK`. Звернення створюється на `/mock/helpdesk`, чернетка з’являється на `/operator`.

### Docker-хостинг

Образ один і той самий для API і воркера. Порт береться зі змінної `PORT`.

- **Два сервіси** (Railway, Fly.io, VPS з `docker compose`): `api` з командою за замовчуванням і `worker` з командою `python -m app.workers.runner`.
- **Один контейнер** (Render, Koyeb): `api` з `EMBEDDED_WORKER=1`. Для Render є `render.yaml`.

## Робота з AI-агентом

Проєкт зроблено з AI-агентами, і їхня конфігурація закомічена:

- `SPEC.md` — специфікація й інваріанти, які агент не має права порушити;
- `AGENTS.md`, `CLAUDE.md` — правила для Claude Code і Cursor;
- `helpdesk-builder/` — окремий агент на Agent SDK з інструментами `webhook_contract` (перевіряє контракт вебхука) і `readiness_checklist` (перевіряє готовність за кодом репозиторію).

Історію комітів залишено як є, без squash.
