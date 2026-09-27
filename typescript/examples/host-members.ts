/**
 * Shows: billing as a host member — one long-lived agent per tenant, shared by every conversation,
 *   addressable by name from any thread. support is a caller: it is in no team, and the one rule
 *   that allows `ask` is the only thing that lets it reach billing, capped at $0.50 per turn it
 *   opens. billing's model gets exactly one team tool, `reply`, since no rule has it as `from`.
 *   A turn of billing that fails ends only that turn: the ask closes failed and the next caller is
 *   answered. `restart` supervises billing's own ends, never a caller's.
 * Needs: ANTHROPIC_API_KEY, SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN
 * Run: threads dev examples/host-members.ts (it exports the host, as README.md does).
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

// read_only, so billing needs no approvers: a shared member that could stop for a human would
// hold every other conversation's mail until someone answered.
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

const support = agent({
  name: "support",
  instructions: "Help customers. For invoice questions, ask billing.",
  model: sonnet,
});

const app: Host = host({
  store: sqlite(".threads"),
  agents: { support, billing },
  // billing runs as one member of the tenant's host team, which never closes. Its history is
  // shared by every conversation, so user-private work belongs in a started member instead.
  members: {
    billing: { restart: "on_failure", maxRestarts: 3, withinMs: 60_000 },
  },
  messagePolicy: [
    // Default deny. A rule to a host member allows only send and ask; start, monitor and cancel
    // are refused at setup, since host({members}) starts it and Host.team stops it.
    {
      from: "support",
      to: "billing",
      allow: ["ask"],
      budget: { max_cost_nanos: usd(0.5) },
    },
  ],
  channels: {
    slack: slack({
      agent: "support",
      signingSecret: secret("SLACK_SIGNING_SECRET"),
      botToken: secret("SLACK_BOT_TOKEN"),
    }),
  },
});

export default app;
