# Lumenboard support drafts

Сервіс приймає звернення підтримки, зберігає їх у PostgreSQL і готує місце для чернетки відповіді оператору. Продукт у базі знань — вигаданий **Lumenboard**.

Зараз працює прийом вебхука, здоров’я сервісу, список звернень і мок відповіді клієнту. Генерація чернетки через LLM, підтвердження оператором і надсилання ще попереду. Контракт і межі — у `SPEC.md`.

## Запуск

Потрібні Docker і Docker Compose.

```bash
docker compose up --build
```

- Здоров’я: [http://localhost:8000/health](http://localhost:8000/health) — тіло `OK`
- Оператор: [http://localhost:8000/operator](http://localhost:8000/operator)
- База: `localhost:5432`, користувач `helpdesk`, пароль `helpdesk`, база `helpdesk`

Зупинка: `docker compose down`.

Приклад звернення через мок хелпдеску:

```bash
curl -X POST http://localhost:8000/mock/helpdesk/tickets ^
  -H "content-type: application/json" ^
  -d "{\"event_id\":\"evt_8f2a\",\"ticket_id\":\"T-1042\",\"customer_email\":\"user@example.com\",\"message\":\"Не можу експортувати звіт у PDF\"}"
```

Той самий вебхук можна надіслати напряму на `POST /webhooks/helpdesk`. Повторний `event_id` не створює другу роботу.

## Змінні

Скопіюйте `.env.example` у `.env`, якщо запускаєте поза Compose. У Compose адреси бази вже задані.

`LLM_API_KEY` поки не використовується. Порожній ключ має лишитися підтриманим режимом: сервіс однаково звертається до провайдера і підставляє статичну чернетку.

## Тести без бази

```bash
python -m pip install -r requirements.txt
python -m pytest
```

Ці тести не піднімають PostgreSQL. Перевірка ідемпотентності вебхука потребує `docker compose up`.

## Агент

`helpdesk-builder/` — агент, який веде цей репозиторій. Інструкції для Cursor і Claude: `AGENTS.md`, `CLAUDE.md`.
