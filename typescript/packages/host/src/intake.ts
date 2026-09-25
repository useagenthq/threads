import {
  type ChannelAdapter,
  Inbound,
  type RawRequest,
  VerifiedDelivery,
} from "@threads/core";
import {
  canonicalize,
  storeConnection,
  type Tx,
  uuidv7,
} from "@threads/core/host";
import type { HostContext } from "./context";
import { failure } from "./errors";
import { threadFor } from "./inbox";

// Channel intake, identical for every channel: verify over the raw bytes,
// parse the whole batch, map the verified identity to tenant and thread, insert every item in
// one transaction, and only then answer the adapter's ack. Nothing runs before the insert.

export type Received = {
  readonly response: Response;
  /** The threads that got new items, for the consumer. */
  readonly threads: readonly {
    readonly tenant: string;
    readonly threadId: string;
  }[];
};

export async function receive(
  ctx: HostContext,
  channel: string,
  request: Request,
): Promise<Received> {
  const adapter = ctx.channels.get(channel);
  if (adapter === undefined)
    return {
      response: failure("not_found", `no channel ${channel}`),
      threads: [],
    };
  const raw = await rawOf(request);
  const verified = adapter.verify(raw);
  const delivery = verified.ok
    ? VerifiedDelivery.safeParse(verified.value)
    : undefined;
  if (delivery?.success !== true)
    return {
      response: failure("unverified", "the webhook failed verification"),
      threads: [],
    };
  const parsed = adapter.parse(raw);
  // A verified batch that doesn't parse will not parse on redelivery either: answer, store nothing.
  if (!parsed.ok) {
    console.warn(
      `threads host: ${channel} batch not parsed: ${parsed.error.message}`,
    );
    return { response: responseOf(adapter, raw), threads: [] };
  }
  const { db } = await storeConnection(ctx.store);
  const threads = await db.transaction((tx) =>
    insertBatch(tx, channel, delivery.data, parsed.value),
  );
  return { response: responseOf(adapter, raw), threads };
}

/** GET /channels/{channel}/events: the adapter's subscription check, if it has one. */
export function challenge(
  ctx: HostContext,
  channel: string,
  request: Request,
): Response {
  const check = ctx.channels.get(channel)?.challenge;
  if (check === undefined)
    return failure("not_found", `no subscription check for ${channel}`);
  const answer = check(Object.fromEntries(new URL(request.url).searchParams));
  if (!answer.ok) return failure("unverified", answer.error.message);
  const { status, headers, body } = answer.value;
  return new Response(Uint8Array.from(body), {
    status,
    headers: { ...headers },
  });
}

async function rawOf(request: Request): Promise<RawRequest> {
  const headers: Record<string, string> = {};
  request.headers.forEach((value, name) => {
    headers[name.toLowerCase()] = value;
  });
  return { headers, body: new Uint8Array(await request.arrayBuffer()) };
}

function responseOf(adapter: ChannelAdapter, raw: RawRequest): Response {
  const ack = adapter.ack(raw);
  return new Response(Uint8Array.from(ack.body), {
    status: ack.status,
    headers: { ...ack.headers },
  });
}

/**
 * Every item of the batch under UNIQUE (tenant, channel, installation, item_key): a redelivered
 * item inserts nothing. An item whose principal is of another tenant than the verified
 * installation's is dropped: message content never selects a tenant.
 */
export async function insertBatch(
  tx: Tx,
  channel: string,
  delivery: VerifiedDelivery,
  items: readonly unknown[],
  now: number = Date.now(),
): Promise<Received["threads"]> {
  const touched = new Map<string, { tenant: string; threadId: string }>();
  for (const candidate of items) {
    const item = Inbound.safeParse(candidate);
    if (!item.success || item.data.kind === "ignore") continue;
    if (item.data.principal.tenant !== delivery.tenant) continue;
    const at = {
      tenant: delivery.tenant,
      channel,
      installation: delivery.installation_id,
      address: item.data.address,
    };
    const threadId = await threadFor(tx, at, uuidv7(now));
    const bytes = canonicalize(item.data);
    if (!bytes.ok) throw new Error("a parsed item is JSON");
    await tx.run(
      `INSERT INTO inbox (tenant_id, channel, installation_id, item_key, delivery_id, thread_id,
        item, received_at, consumed_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT DO NOTHING`,
      [
        delivery.tenant,
        channel,
        delivery.installation_id,
        item.data.item_key,
        delivery.delivery_id,
        threadId,
        new TextEncoder().encode(bytes.value),
        now,
      ],
    );
    touched.set(threadId, { tenant: delivery.tenant, threadId });
  }
  return [...touched.values()];
}
