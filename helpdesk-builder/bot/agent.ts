import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineAgent } from "@grok/agent-sdk";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

export default defineAgent({
  name: "helpdesk-builder",
  description:
    "Implements the Lumenboard support-draft backend and checks webhook and readiness contracts.",
  model: {
    id: "grok-4.7",
    params: [
      { id: "reasoning_effort", value: "high" },
      { id: "fast", value: "true" },
    ],
  },
  local: {
    workspaceDir: repoRoot,
  },
});
