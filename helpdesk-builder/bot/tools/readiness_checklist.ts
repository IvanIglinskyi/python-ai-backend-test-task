import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineTool } from "@grok/agent-sdk/tools";
import * as z from "zod/v4";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");

function read(relativePath: string): string {
  try {
    return readFileSync(path.join(repoRoot, relativePath), "utf8");
  } catch {
    return "";
  }
}

type Check = {
  id: string;
  title: string;
  done: boolean;
  evidence: string;
};

function check(id: string, title: string, done: boolean, evidence: string): Check {
  return { id, title, done, evidence };
}

export default defineTool({
  description:
    "Read the backend repo and report which assignment checks already pass. Use this before calling the work finished.",
  inputSchema: z.object({}),
  effect: "read",
  async execute() {
    const main = read("app/main.py");
    const webhooks = read("app/api/webhooks.py");
    const ingest = read("app/services/ingest.py");
    const compose = read("docker-compose.yml");
    const articles = read("app/kb/articles.json");
    const llm = read("app/services/llm.py");
    const operator = read("app/api/operator.py");
    const worker = read("app/workers/runner.py");
    const delivery = read("app/services/delivery.py");
    let articleCount = 0;
    try {
      const parsed: unknown = JSON.parse(articles);
      articleCount = Array.isArray(parsed) ? parsed.length : 0;
    } catch {
      articleCount = 0;
    }

    const items = [
      check(
        "health",
        "GET /health returns 200 and the text OK",
        main.includes('"/health"') && main.includes("OK"),
        "app/main.py",
      ),
      check(
        "webhook",
        "POST /webhooks/helpdesk accepts the helpdesk body",
        webhooks.includes('"/webhooks/helpdesk"'),
        "app/api/webhooks.py",
      ),
      check(
        "idempotent_event",
        "The same event_id is stored once",
        ingest.includes("on_conflict_do_nothing") && ingest.includes("event_id"),
        "app/services/ingest.py",
      ),
      check(
        "durable_queue",
        "Work waits in PostgreSQL",
        compose.includes("postgres:16") && read("app/db/models.py").includes("class Job"),
        "docker-compose.yml and app/db/models.py",
      ),
      check(
        "knowledge_base",
        "At least five knowledge-base articles are searchable",
        articleCount >= 5 && read("app/kb/search.py").includes("def search_articles"),
        `app/kb/articles.json (${articleCount})`,
      ),
      check(
        "llm_modes",
        "Drafting calls the provider and can fall back to a delayed static draft",
        llm.includes("httpx") && llm.includes("static"),
        "app/services/llm.py",
      ),
      check(
        "rate_limit",
        "The service stays within 60 model calls a minute",
        llm.includes("60") || read("app/services/rate_limit.py").includes("60"),
        "llm or rate_limit module",
      ),
      check(
        "operator_decision",
        "An operator can approve or reject a draft",
        operator.includes("approve") && operator.includes("reject"),
        "app/api/operator.py",
      ),
      check(
        "single_delivery",
        "A confirmed reply is posted once, with retries on the same delivery",
        delivery.includes("Idempotency-Key") && delivery.includes("sent"),
        "app/services/delivery.py",
      ),
      check(
        "worker_claim",
        "The worker claims jobs with SKIP LOCKED",
        worker.includes("SKIP LOCKED"),
        "app/workers/runner.py",
      ),
    ];

    return {
      ready: items.every((item) => item.done),
      items,
    };
  },
});
