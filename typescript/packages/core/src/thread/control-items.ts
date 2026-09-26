import { z } from "zod";
import { Inbound } from "../channel/protocol";
import { canonicalize, Int, type Principal, type ThreadId, Uuid } from "../log";
import type { StoreDriver, Tx } from "../store/driver";
import { reading } from "../store/driver";
import { uuidv7 } from "../store/encode";

// Thread.cancel across processes (lane 29F): a control item in the `inbox` that the branch's
// lease holder applies at its next step boundary, so a cancel is durable whoever runs the
// thread. It is the intake that already exists, under the reserved channel `api`, where
// `address` is the thread id, nothing is delivered and only `cancel` and `stop_when_idle`
// apply. The requester's authority is checked before the row is written, because applying an
// item never checks again.

/** The reserved channel of a control item no channel adapter owns. */
export const API_CHANNEL = "api";
const API_INSTALLATION = "local";

export type ControlCommand = "cancel" | "stop_when_idle";

/** host-api CancelAccepted: a cancel another process will apply, named by its item key. */
export type CancelAccepted = { readonly item_key: string };

const ControlRow = z.strictObject({
  inbox_id: Int,
  item_key: z.string(),
  item: z.instanceof(Uint8Array),
});

/** One unconsumed control item of a thread. */
export type ControlItem = {
  readonly inboxId: number;
  readonly itemKey: string;
  readonly command: ControlCommand;
  readonly principal: Principal;
};

const utf8 = new TextDecoder();

/**
 * The durable control item, by its key. One transaction: an unconsumed item of the same command
 * is the answer, so a caller that asks again (a parent barring a busy child at every step) never
 * queues a second barrier for the same thread.
 */
export async function writeControlItem(
  db: StoreDriver,
  tenant: string,
  threadId: ThreadId,
  principal: Principal,
  command: ControlCommand,
  now: number,
): Promise<CancelAccepted> {
  return db.transaction(async (tx) => {
    const waiting = (await pendingControlItems(tx, threadId)).find(
      (i) => i.command === command,
    );
    if (waiting !== undefined) return { item_key: waiting.itemKey };
    const itemKey = Uuid.parse(uuidv7(now));
    const bytes = canonicalize({
      kind: "control",
      principal,
      address: threadId,
      item_key: itemKey,
      command,
    });
    if (!bytes.ok) throw new Error("a control item is canonical JSON");
    await tx.run(
      `INSERT INTO inbox (tenant_id, channel, installation_id, item_key, delivery_id, thread_id,
        item, received_at, consumed_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT DO NOTHING`,
      [
        tenant,
        API_CHANNEL,
        API_INSTALLATION,
        itemKey,
        itemKey,
        threadId,
        new TextEncoder().encode(bytes.value),
        now,
      ],
    );
    return { item_key: itemKey };
  });
}

/**
 * The thread's unconsumed `api` control items, oldest first. Read inside the transaction that
 * applies them, so what is read is what is consumed. A thread id is unique across the store, so
 * the tenant is not part of the lookup.
 */
export async function pendingControlItems(
  tx: Tx,
  threadId: ThreadId,
): Promise<readonly ControlItem[]> {
  const rows = z.array(ControlRow).parse(
    await tx.all(
      `SELECT inbox_id, item_key, item FROM inbox WHERE thread_id = ? AND channel = ?
        AND consumed_seq IS NULL ORDER BY inbox_id`,
      [threadId, API_CHANNEL],
    ),
  );
  return rows.map((row) => {
    const item = Inbound.parse(JSON.parse(utf8.decode(row.item)));
    // The `api` channel carries control items only; anything else is a broken store invariant.
    if (item.kind !== "control")
      throw new Error(`an ${API_CHANNEL} inbox item is a control item`);
    return {
      inboxId: row.inbox_id,
      itemKey: row.item_key,
      command: item.command,
      principal: item.principal,
    };
  });
}

/** Whether a control item waits for this thread: the holder's cheap check at each boundary. */
export async function controlItemsPending(
  db: StoreDriver,
  threadId: ThreadId,
): Promise<boolean> {
  const rows = await reading(db, (tx) =>
    tx.all(
      "SELECT 1 FROM inbox WHERE thread_id = ? AND channel = ? AND consumed_seq IS NULL LIMIT 1",
      [threadId, API_CHANNEL],
    ),
  );
  return rows.length > 0;
}

/**
 * Marks the items consumed at `seq`, the first event of the append that applies them, under the
 * `consumed_seq IS NULL` CAS. False when another owner applied one first: the caller rolls its
 * append back, so an item is never applied twice.
 */
export async function consumeControlItems(
  tx: Tx,
  items: readonly ControlItem[],
  seq: number,
): Promise<boolean> {
  for (const item of items) {
    const changed = await tx.run(
      "UPDATE inbox SET consumed_seq = ? WHERE inbox_id = ? AND consumed_seq IS NULL",
      [seq, item.inboxId],
    );
    if (changed !== 1) return false;
  }
  return true;
}
