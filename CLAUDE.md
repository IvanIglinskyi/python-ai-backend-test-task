# Claude / Cursor

Follow `AGENTS.md` and `SPEC.md`.

The service is FastAPI and PostgreSQL. The webhook only stores the event. The worker calls the model. A reply goes to the customer only after the operator approves it, and only once.

Never call the LLM inside the webhook's HTTP request. Never lose an `event_id` on redelivery.

The rules that are easiest to break (details in `AGENTS.md`):

- Commits are authored by `IvanIglinskyi`, with no `Co-Authored-By` lines or any other AI attribution. Small logical commits, no squashing.
- Secrets (`LLM_API_KEY`, `CRON_SECRET`, `DATABASE_URL`) never go into git, command output or instructions.
- A `429` from the LLM provider means "wait", not "fail": it does not spend an attempt.
- The UI and the knowledge base are in English; customers may write in Ukrainian.
