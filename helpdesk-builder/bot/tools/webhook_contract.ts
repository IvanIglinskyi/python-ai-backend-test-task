import { defineTool } from "@grok/agent-sdk/tools";
import * as z from "zod/v4";

const payloadSchema = z.object({
  event_id: z.string().min(1).max(128),
  ticket_id: z.string().min(1).max(128),
  customer_email: z.string().email(),
  message: z.string().min(1),
  reply_url: z.string().url(),
});

export default defineTool({
  description:
    "Check a helpdesk webhook body against the assignment contract and return the ack rules. Use this before changing ingest.",
  inputSchema: payloadSchema,
  effect: "read",
  async execute(payload) {
    const parsed = payloadSchema.safeParse(payload);
    if (!parsed.success) {
      return {
        valid: false,
        errors: parsed.error.issues.map((issue) => issue.message),
        ack: null,
      };
    }
    return {
      valid: true,
      errors: [],
      ack: {
        maxResponseMs: 2000,
        persistBeforeAck: true,
        duplicateKey: "event_id",
        httpStatus: 200,
      },
      normalized: parsed.data,
    };
  },
});
