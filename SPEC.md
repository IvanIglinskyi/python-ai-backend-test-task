# Специфікація: чернетки підтримки Lumenboard

Стек: FastAPI, PostgreSQL 16, Docker Compose. Один процес приймає HTTP, другий (`python -m app.workers.runner`) забирає роботу з бази.

## Інваріанти

1. `POST /webhooks/helpdesk` відповідає швидше ніж за 2 секунди. У запиті немає виклику LLM.
2. Подія зберігається до відповіді вебхука. Повтор і паралельна доставка з тим самим `event_id` дають один рядок `webhook_events` і одну роботу `jobs`.
3. Звернення не відкидаються, коли воркер зайнятий. Пік — до 500 подій за 10 хвилин. Черга живе в PostgreSQL.
4. Викликів LLM не більше 60 на хвилину на весь сервіс.
5. Без `LLM_API_KEY` клієнт усе одно робить HTTP-запит до провайдера, ігнорує тіло відповіді і після затримки 5–30 секунд зберігає статичну чернетку. Далі система працює з нею так само, як із живою.
6. Клієнт отримує підтверджену відповідь один раз. Повторна доставка після 5xx або таймауту йде з тим самим `Idempotency-Key`. Рядок `deliveries` у стані `sent` більше не відправляється.
7. `GET /health` завжди віддає `200` і текст `OK`.

## Контракт хелпдеску

Вхід, `POST /webhooks/helpdesk`:

```json
{
  "event_id": "evt_8f2a",
  "ticket_id": "T-1042",
  "customer_email": "user@example.com",
  "message": "Не можу експортувати звіт у PDF",
  "reply_url": "https://helpdesk.example.com/tickets/T-1042/replies"
}
```

Відповідь клієнту, `POST <reply_url>`:

```json
{
  "ticket_id": "T-1042",
  "message": "Щоб експортувати звіт у PDF, відкрийте..."
}
```

Успіх хелпдеску: `201`. Заголовок `Idempotency-Key` обов’язковий. МОК уже вимагає його на `POST /mock/helpdesk/tickets/{ticket_id}/replies` і не пише другий рядок для того самого ключа.

## База знань

Вісім статей у `app/kb/articles.json`. Пошук лексичний, `app/kb/search.py`. Окремий векторний індекс не потрібен.

## Статуси

`tickets.status`: `queued`, далі `drafting`, `pending_review`, `approved`, `rejected`, `sending`, `sent`, `send_failed`. `draft_failed` — LLM не відповів після `JOB_MAX_ATTEMPTS` спроб.

`drafts.status`: `pending_review`, `approved`, `rejected`.

`deliveries.state`: `pending`, `in_flight`, `sent`, `failed`.

`jobs.status`: `queued`, `running`, `done`, `failed`.

## Черга

Воркер забирає роботу так: `UPDATE jobs ... WHERE id = (SELECT id ... FOR UPDATE SKIP LOCKED LIMIT 1)` для `status IN (queued, running)` і `available_at <= now()`. Для `running` поле `available_at` — це кінець лізингу: роботу впалого воркера забирає інший. `attempts` — fencing token, результат записує лише останній власник. Доставки забираються так само з таблиці `deliveries`.

## Реалізація

| Пункт | Де |
|---|---|
| Прийом вебхука | `app/services/ingest.py` |
| Забирання робіт, генерація, повтори | `app/services/drafting.py`, `app/workers/runner.py` |
| LLM-клієнт, режим без ключа | `app/services/llm.py` |
| Ліміт 60/хв у базі (`llm_calls`, advisory lock) | `app/services/rate_limit.py` |
| Підтвердити, відхилити, повторити доставку | `app/api/operator.py`, `app/services/delivery.py` |
| Відправник з `Idempotency-Key = lumenboard-draft-<draft_id>` | `app/services/delivery.py` |
| Нестабільний мок (`MOCK_FAILURE_RATE`, `MOCK_SLOW_RATE`) | `app/api/mock_helpdesk.py` |
| Тести паралельних дублікатів і однієї доставки | `tests/test_integration.py` |

Поза цим контрактом нічого не надсилати клієнту.
