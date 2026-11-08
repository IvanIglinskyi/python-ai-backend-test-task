# Repository agent

This repository is a take-home assignment: a support-draft backend for the Lumenboard product.

Read `SPEC.md` before changing the webhook, the queue, the LLM client or delivery. Its invariants outrank local convenience.

Product code lives in `app/`. `helpdesk-builder/` is a separate Agent SDK agent. Do not move FastAPI into it and do not put secrets into its instructions.

Do not mark a step done until a test or a `GET /health` check covers it. Commit only when explicitly asked, and never squash history.

## Commits

- Author and committer are `IvanIglinskyi` (`git config user.name`). Do not add `Co-Authored-By`, "Generated with …" or any other AI attribution to commits or PRs.
- Small logical commits: feature, fix, tests and docs separately. Messages in English; the body explains why, not only what.
- Before committing, keep the working tree clean: `.env`, `.env.local` and `.vercel/` never go into git.

## Verification

- `python -m pytest` — unit tests without a database (integration tests are skipped without `TEST_DATABASE_URL`).
- `docker compose run --rm tests` — the full suite against PostgreSQL in a separate `helpdesk_test` database.
- `python e2e/ui_check.py <url> [reply-base-url]` — Playwright clicks every button. Against compose, pass `http://api:8000` as the second argument.
- CI (`.github/workflows/tests.yml`) runs the same suite on `postgres:16` on every push.
- After UI changes, run `e2e/ui_check.py`: HTTP-level tests do not catch layout problems (for example, overflow at 390 px).

## Secrets and deployment

- Keys live only in `.env` (local, git-ignored) and in Vercel environment variables. Never print them; check length or prefix at most.
- Production: `npx vercel deploy --prod` from the repository root. Vercel picks up new variables only on the next deploy.
- `vercel.json` has no rewrite: Vercel detects FastAPI itself, and a rewrite turns every path into `/api/index`.
- Bulk deletes in the production database only with the user's explicit permission.

## UI

- Jinja2 in `app/templates`, Tailwind and htmx. The libraries and the font are vendored in `app/static/vendor`; do not go back to CDNs — on a slow network the page waited ~30 s for them.
- UI text and knowledge-base articles are in English. Customers may write in Ukrainian: search understands Cyrillic through `keywords_uk` in the articles.
- Every action works both through htmx (returns the card) and without JavaScript (303 to `/operator`).

## Local environment (Windows)

- `docker compose` publishes ports on `127.0.0.1`: with Docker Desktop on WSL2, `localhost` goes to `::1` first and the IPv6 port forward hung.
- In Git Bash, path-like arguments such as `/health` become `C:/Program Files/Git/health`; set `MSYS_NO_PATHCONV=1` for such commands.
- Send Cyrillic request bodies from a file (`--data-binary @file.json`), not as a command-line argument.
