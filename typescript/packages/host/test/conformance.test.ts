import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  agent,
  type ChannelAdapter,
  scriptedModel,
  sqlite,
} from "@threads/core";
import {
  knownEvents,
  openStore,
  Principal,
  storeConnection,
  tenantStore,
} from "@threads/core/host";
import { z } from "zod";
import {
  CASE_NAMES,
  CASES_DIR,
  loadCase,
} from "../../core/test/conformance/cases";
import { host } from "../src";
import { sqlAll } from "./sql";

// The host's conformance runner: every `intake` and `host` case of spec/conformance/cases, run
// as the README's "What a runner does per kind" says, with no per-case code.

const Webhook = z.strictObject({
  channel: z.string(),
  installation_id: z.string(),
  delivery_id: z.string(),
  forged: z.boolean().optional(),
  items: z.array(
    z.strictObject({
      item_id: z.string().optional(),
      conversation: z.string(),
      sender: z.string(),
      text: z.string(),
    }),
  ),
});
const IntakeInput = z.strictObject({ webhooks: z.array(Webhook) });
const IntakeExpected = z.strictObject({
  outcome: z.literal("ok"),
  responses: z.array(z.int()),
  inbox: z.array(z.strictObject({ channel: z.string(), item_key: z.string() })),
  threads: z.int().optional(),
});
const HostInput = z.strictObject({
  requests: z.array(
    z.strictObject({
      principal: Principal,
      idempotency_key: z.string(),
      body: z.record(z.string(), z.unknown()),
    }),
  ),
});
const HostExpected = z.strictObject({
  outcome: z.literal("ok"),
  api: z.array(
    z.strictObject({
      status: z.int(),
      receipt: z.int().optional(),
      code: z.string().optional(),
    }),
  ),
  user_inputs: z.int(),
});
const Receipt = z.strictObject({
  thread_id: z.string(),
  branch_id: z.string(),
  run_id: z.string(),
});

function expected(name: string): unknown {
  return JSON.parse(
    readFileSync(join(CASES_DIR, name, "expected.json"), "utf8"),
  );
}

function demo(): ReturnType<typeof agent> {
  const done = {
    content: [{ type: "text", text: "Done." }],
    stop_reason: "end_turn",
  };
  return agent({
    name: "demo",
    model: scriptedModel({
      responses: Array.from({ length: 10 }, () => ({
        ...done,
        usage: { input_tokens: 1, output_tokens: 1 },
      })),
    }),
  });
}

/** The README's fake verifying adapter: the webhook JSON is the delivery. */
function fakeAdapter(): ChannelAdapter {
  const parsed = (raw: { readonly body: Uint8Array }) =>
    Webhook.parse(JSON.parse(new TextDecoder().decode(raw.body)));
  return {
    agent: "demo",
    capabilities: {
      lookup: "none",
      buttons: false,
      edits: false,
      files: false,
      direct_messages: false,
    },
    limits: {},
    secrets: {},
    verify: (raw) => {
      const w = parsed(raw);
      if (w.forged === true)
        return { ok: false, error: { code: "unverified", message: "forged" } };
      return {
        ok: true,
        value: {
          tenant: `${w.channel}:${w.installation_id}`,
          installation_id: w.installation_id,
          delivery_id: w.delivery_id,
        },
      };
    },
    parse: (raw) => {
      const w = parsed(raw);
      const tenant = `${w.channel}:${w.installation_id}`;
      return {
        ok: true,
        value: w.items.map((item, index) => ({
          kind: "message",
          principal: { issuer: tenant, tenant, subject: item.sender },
          address: item.conversation,
          item_key: item.item_id ?? `${w.delivery_id}#${index}`,
          content: item.text,
        })),
      };
    },
    ack: () => ({ status: 200, headers: {}, body: new Uint8Array() }),
    render: () => [],
    renderText: () => [],
    perform: async () => ({
      status: "delivery_error",
      kind: "permanent",
      sent: "definite_not_sent",
    }),
    lookup: async () => ({ status: "unknown", reason: "no lookup" }),
  };
}

async function runIntake(name: string, input: unknown): Promise<void> {
  const { webhooks } = IntakeInput.parse(input);
  const want = IntakeExpected.parse(expected(name));
  const store = sqlite(":memory:");
  const channels = Object.fromEntries(
    webhooks.map((w) => [w.channel, fakeAdapter()]),
  );
  const h = host({ store, agents: { demo: demo() }, channels });
  const responses: number[] = [];
  for (const w of webhooks) {
    const response = await h.fetch(
      new Request(`http://host.test/channels/${w.channel}/events`, {
        method: "POST",
        body: JSON.stringify(w),
      }),
    );
    responses.push(response.status);
  }
  const { db } = await storeConnection(store);
  const rows = z
    .array(
      z.strictObject({
        channel: z.string(),
        item_key: z.string(),
        thread_id: z.string(),
      }),
    )
    .parse(
      await sqlAll(
        db,
        "SELECT channel, item_key, thread_id FROM inbox ORDER BY inbox_id",
        [],
      ),
    );
  expect(responses).toEqual(want.responses);
  expect(rows.map(({ channel, item_key }) => ({ channel, item_key }))).toEqual(
    want.inbox,
  );
  if (want.threads !== undefined)
    expect(new Set(rows.map((r) => r.thread_id)).size).toBe(want.threads);
  await h.stop();
}

async function runHost(name: string, input: unknown): Promise<void> {
  const { requests } = HostInput.parse(input);
  const want = HostExpected.parse(expected(name));
  const store = sqlite(":memory:");
  const h = host({
    store,
    agents: { demo: demo() },
    authenticate: async (r) =>
      Principal.parse(JSON.parse(r.headers.get("x-principal") ?? "null")),
  });
  const bodies: string[] = [];
  const statuses: number[] = [];
  for (const r of requests) {
    const response = await h.fetch(
      new Request("http://host.test/v1/runs", {
        method: "POST",
        headers: {
          "x-principal": JSON.stringify(r.principal),
          "idempotency-key": r.idempotency_key,
        },
        body: JSON.stringify(r.body),
      }),
    );
    statuses.push(response.status);
    bodies.push(await response.text());
  }
  await h.stop();
  const receipts = compare(want, statuses, bodies);
  const tenants = new Set(requests.map((r) => r.principal.tenant));
  expect(await inputsOf(store, receipts, tenants)).toBe(want.user_inputs);
}

/** Checks each answer; returns the distinct 202 receipts. */
function compare(
  want: z.infer<typeof HostExpected>,
  statuses: readonly number[],
  bodies: readonly string[],
): ReadonlySet<string> {
  const receipts = new Set<string>();
  for (const [i, answer] of want.api.entries()) {
    expect(statuses[i]).toBe(answer.status);
    const body = JSON.parse(bodies[i] ?? "null");
    if (answer.receipt !== undefined) {
      expect(body).toEqual(JSON.parse(bodies[answer.receipt] ?? "null"));
      receipts.add(bodies[i] ?? "");
    }
    if (answer.code !== undefined) expect(body.error.code).toBe(answer.code);
  }
  // A failure never carries another request's receipt.
  const runIds = [...receipts].map((r) => Receipt.parse(JSON.parse(r)).run_id);
  for (const [i, answer] of want.api.entries())
    if (answer.code !== undefined)
      for (const runId of runIds) expect(bodies[i]).not.toContain(runId);
  return receipts;
}

/** user_input events in the logs of every thread a receipt names. */
async function inputsOf(
  store: ReturnType<typeof sqlite>,
  receipts: ReadonlySet<string>,
  tenants: ReadonlySet<string>,
): Promise<number> {
  let inputs = 0;
  for (const receipt of receipts) {
    const branch = z
      .string()
      .brand<"BranchId">()
      .parse(Receipt.parse(JSON.parse(receipt)).branch_id);
    for (const tenant of tenants) {
      const { log } = await openStore(tenantStore(store, tenant));
      const read = await log.read(branch);
      if (read.ok)
        inputs += knownEvents(read.value).filter(
          (e) => e.type === "user_input",
        ).length;
    }
  }
  return inputs;
}

describe("host conformance", () => {
  for (const name of CASE_NAMES) {
    const c = loadCase(name);
    if (c.kind === "intake")
      test(`intake: ${name}`, () => runIntake(name, c.input));
    if (c.kind === "host") test(`host: ${name}`, () => runHost(name, c.input));
  }
});
