import {
  type Agent,
  agent,
  type ChannelAdapter,
  type DeliveryOutcome,
  type Inbound,
  type Model,
  type Store,
  scriptedModel,
  secret,
  sqlite,
  tool,
} from "@threads/core";
import {
  type KnownEvent,
  knownEvents,
  openStore,
  type Principal,
  Principal as PrincipalSchema,
  tenantStore,
} from "@threads/core/host";
import { z } from "zod";
import { type Host, type HostOptions, host } from "../src";

// Shared test kit: scripted agents, a header-based authenticate, and a fake channel adapter whose
// verify, perform and lookup are scripted. No network anywhere.

const usage = { input_tokens: 10, output_tokens: 2 };

export function say(text: string): unknown {
  return { content: [{ type: "text", text }], stop_reason: "end_turn", usage };
}

export function use(
  name: string,
  input: Record<string, unknown>,
  id: string,
): unknown {
  return {
    content: [{ type: "tool_use", call_id: id, name, input }],
    stop_reason: "tool_use",
    usage,
  };
}

export const alice: Principal = {
  issuer: "api",
  tenant: "acme",
  subject: "alice",
};
export const bob: Principal = { issuer: "api", tenant: "acme", subject: "bob" };
export const eve: Principal = {
  issuer: "api",
  tenant: "other",
  subject: "eve",
};

/** The principal named by the x-principal header; no header is unauthenticated. */
export async function authenticate(
  request: Request,
): Promise<Principal | null> {
  const header = request.headers.get("x-principal");
  return header === null ? null : PrincipalSchema.parse(JSON.parse(header));
}

export function mailer(options: {
  readonly responses: readonly unknown[];
  readonly approvers?: readonly Principal[];
  readonly sent?: string[];
  /** In place of a scripted model of `responses`. */
  readonly model?: Model;
}): Agent<undefined, string> {
  const send = tool({
    name: "send_email",
    description: "Send an email.",
    input: z.object({ to: z.string() }),
    runs: "host",
    execute: async ({ to }) => {
      options.sent?.push(to);
      return "sent";
    },
  });
  return agent({
    name: "support",
    model:
      options.model ?? scriptedModel({ responses: [...options.responses] }),
    tools: [send],
    ...(options.approvers === undefined
      ? {}
      : { approvers: options.approvers }),
  });
}

export type Harness = {
  readonly host: Host;
  readonly store: Store;
  readonly call: (
    method: string,
    path: string,
    options?: {
      readonly as?: Principal;
      readonly body?: unknown;
      readonly headers?: Record<string, string>;
    },
  ) => Promise<Response>;
};

export function harness(
  options: Omit<HostOptions, "store"> & { readonly store?: Store },
): Harness {
  const store = options.store ?? sqlite(":memory:");
  const h = host({ store, authenticate, ...options });
  return {
    host: h,
    store,
    call: (method, path, o = {}) =>
      h.fetch(
        new Request(`http://host.test${path}`, {
          method,
          headers: {
            ...(o.as === undefined
              ? {}
              : { "x-principal": JSON.stringify(o.as) }),
            ...(o.body === undefined
              ? {}
              : { "content-type": "application/json" }),
            ...o.headers,
          },
          ...(o.body === undefined ? {} : { body: JSON.stringify(o.body) }),
        }),
      ),
  };
}

/** The tenant's branch events, read back through the store. */
export async function eventsOf(
  store: Store,
  tenant: string,
  branch: string,
): Promise<
  readonly { readonly type: string; readonly [k: string]: unknown }[]
> {
  return knownEventsOf(store, tenant, branch);
}

/** The tenant's branch events as their typed union. */
export async function knownEventsOf(
  store: Store,
  tenant: string,
  branch: string,
): Promise<readonly KnownEvent[]> {
  const { log } = await openStore(tenantStore(store, tenant));
  const read = await log.read(z.string().brand<"BranchId">().parse(branch));
  if (!read.ok) throw new Error(read.error.message);
  return knownEvents(read.value);
}

/** Reads a whole SSE body into its data messages. */
export async function sseMessages(
  response: Response,
): Promise<readonly unknown[]> {
  const text = await response.text();
  return text
    .split("\n\n")
    .filter((block) => block.includes("data: "))
    .map((block) => JSON.parse(block.slice(block.indexOf("data: ") + 6)));
}

export type FakeChannel = ChannelAdapter & {
  readonly performed: { readonly op: unknown; readonly key: string }[];
  outcomes: DeliveryOutcome[];
  lookups: ("found" | "not_found" | "unknown")[];
};

/**
 * A channel whose webhook body is JSON {tenant, installation_id, delivery_id, items: Inbound[]}
 * and whose signature header must be "good".
 */
export function fakeChannel(
  agentKey: string,
  capabilities: Partial<ChannelAdapter["capabilities"]> = {},
): FakeChannel {
  process.env["FAKE_CHANNEL_TOKEN"] = "token-value";
  const body = (raw: { readonly body: Uint8Array }) =>
    z
      .object({
        tenant: z.string(),
        installation_id: z.string(),
        delivery_id: z.string(),
        items: z.array(z.unknown()),
      })
      .parse(JSON.parse(new TextDecoder().decode(raw.body)));
  const channel: FakeChannel = {
    agent: agentKey,
    capabilities: {
      lookup: "final",
      buttons: true,
      edits: false,
      files: false,
      direct_messages: true,
      ...capabilities,
    },
    limits: {},
    secrets: { token: secret("FAKE_CHANNEL_TOKEN") },
    performed: [],
    outcomes: [],
    lookups: [],
    verify: (raw) => {
      if (raw.headers["x-signature"] !== "good")
        return {
          ok: false,
          error: { code: "unverified", message: "bad signature" },
        };
      const { tenant, installation_id, delivery_id } = body(raw);
      return { ok: true, value: { tenant, installation_id, delivery_id } };
    },
    parse: (raw) => {
      // The fake passes items through; the host parses them as Inbound (a boundary).
      const items = z.array(z.custom<Inbound>()).parse(body(raw).items);
      return { ok: true, value: items };
    },
    ack: () => ({
      status: 200,
      headers: {},
      body: new TextEncoder().encode("ok"),
    }),
    render: (event) => {
      if (event.type === "model_response")
        return [
          {
            kind: "text",
            text: event.data.content
              .map((p) => (p.type === "text" ? p.text : ""))
              .join(""),
          },
        ];
      if (event.type === "approval_requested")
        return [{ kind: "approval", challenge_id: event.data.challenge_id }];
      return [];
    },
    renderText: (text) => [{ kind: "text", text }],
    perform: async (op, key, credentials) => {
      if (credentials["token"] !== "token-value")
        throw new Error("no credentials");
      channel.performed.push({ op, key });
      return (
        channel.outcomes.shift() ?? {
          status: "sent",
          platform_ref: `msg-${key}`,
        }
      );
    },
    lookup: async (key) => {
      const next = channel.lookups.shift() ?? "not_found";
      if (next === "found") return { status: "found", value: `found-${key}` };
      if (next === "unknown") return { status: "unknown", reason: "down" };
      return { status: "not_found" };
    },
  };
  return channel;
}

export function webhook(
  body: {
    readonly tenant: string;
    readonly installation_id: string;
    readonly delivery_id: string;
    readonly items: readonly unknown[];
  },
  signature = "good",
): { readonly body: unknown; readonly headers: Record<string, string> } {
  return { body, headers: { "x-signature": signature } };
}

/** Waits until `check` holds, polling; fails the test after `ms`. */
export async function until(
  check: () => Promise<boolean>,
  ms = 3_000,
): Promise<void> {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (await check()) return;
    await Bun.sleep(20);
  }
  throw new Error("timed out waiting");
}
