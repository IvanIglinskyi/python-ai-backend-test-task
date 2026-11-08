import { defineEval, includes } from "@grok/agent-sdk/evals";

export default defineEval({
  tags: ["smoke"],
  description: "A valid helpdesk payload is checked before the agent answers.",
  async test(t) {
    await t.send(
      'Call webhook_contract for event_id evt_8f2a, ticket_id T-1042, customer_email user@example.com, message "Не можу експортувати звіт у PDF", reply_url https://helpdesk.example.com/tickets/T-1042/replies. Then say whether the webhook may acknowledge it.',
    );
    t.succeeded();
    t.calledTool("webhook_contract");
    t.check(t.reply, includes(/ack|event_id|valid/i));
  },
});
