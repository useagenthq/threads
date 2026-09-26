/**
 * Shows: three host agents, one explicit Slack route, and a messagePolicy that lets support start
 *   and ask billing (each started member capped at $2.00). Everything else is denied and recorded
 *   as message_policy_decided. support has no `team` of its own: the rule that allows `start` is
 *   what makes it a lead, and its model gets exactly the two team tools its rules allow.
 * Needs: ANTHROPIC_API_KEY, SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN
 * Run: threads dev examples/host-policy.ts (it exports the host, as README.md does).
 */
import { anthropic } from "@threads/anthropic";
import { agent, secret, sqlite, tool, usd } from "@threads/core";
import { type Host, host } from "@threads/host";
import { slack } from "@threads/slack";
import { z } from "zod";

const INVOICES: ReadonlyMap<string, string> = new Map([
  ["INV-1001", "paid"],
  ["INV-1002", "overdue"],
]);

const invoiceStatus = tool({
  name: "invoice_status",
  description: "Look up an invoice's status by id, e.g. INV-1001.",
  input: z.object({ id: z.string().regex(/^INV-\d{4}$/) }),
  runs: "host",
  effect: "read_only",
  execute: async ({ id }) => INVOICES.get(id) ?? "unknown invoice",
});

const sonnet = anthropic("claude-sonnet-5", { maxTokens: 2048 });

const billing = agent({
  name: "billing",
  instructions:
    "Answer invoice questions with invoice_status. Answer asks with reply.",
  model: sonnet,
  tools: [invoiceStatus],
});

const hr = agent({
  name: "hr",
  instructions: "Answer HR policy questions.",
  model: sonnet,
});

const support = agent({
  name: "support",
  instructions:
    "Help customers. For invoice questions, start billing and ask it.",
  model: sonnet,
});

const app: Host = host({
  store: sqlite(".threads"),
  agents: { support, billing, hr },
  channels: {
    // Three host agents: the channel must name its agent.
    slack: slack({
      agent: "support",
      signingSecret: secret("SLACK_SIGNING_SECRET"),
      botToken: secret("SLACK_BOT_TOKEN"),
    }),
  },
  messagePolicy: [
    // Default deny. support -> hr, billing -> support, and any cancel are refused and recorded.
    {
      from: "support",
      to: "billing",
      allow: ["start", "ask"],
      budget: { max_cost_nanos: usd(2.0) },
    },
  ],
});

export default app;
