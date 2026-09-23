import { Inbound } from "@threads/core";
import {
  Int,
  type LogError,
  parseRows,
  type Result,
  type SqliteDriver,
  ThreadId,
} from "@threads/core/host";
import { z } from "zod";

// store.sql inbox and channel_threads: the durable intake a run consumes from under the branch
// lease.

export type Conversation = {
  readonly tenant: string;
  readonly channel: string;
  readonly installation: string;
  readonly address: string;
};

const ThreadRow: z.ZodType<{ readonly thread_id: ThreadId }> = z.strictObject({
  thread_id: ThreadId,
});

/** The conversation's thread: an atomic create-or-get, keyed by the verified identity only. */
export function threadFor(
  db: SqliteDriver,
  at: Conversation,
  fresh: string,
): ThreadId {
  db.run(
    `INSERT INTO channel_threads (tenant_id, channel, installation_id, address, thread_id)
      VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING`,
    [at.tenant, at.channel, at.installation, at.address, fresh],
  );
  const rows = parseRows(
    ThreadRow,
    db.all(
      `SELECT thread_id FROM channel_threads WHERE tenant_id = ? AND channel = ?
        AND installation_id = ? AND address = ?`,
      [at.tenant, at.channel, at.installation, at.address],
    ),
  );
  const row = rows.ok ? rows.value[0] : undefined;
  if (row === undefined)
    throw new Error("channel_threads holds the row just written");
  return row.thread_id;
}

export type InboxItem = {
  readonly inbox_id: number;
  readonly channel: string;
  readonly installation_id: string;
  readonly delivery_id: string;
  readonly received_at: number;
  readonly item: Exclude<Inbound, { kind: "ignore" }>;
};

const Row = z.strictObject({
  inbox_id: Int,
  channel: z.string(),
  installation_id: z.string(),
  delivery_id: z.string(),
  received_at: Int,
  item: z.instanceof(Uint8Array),
});

/** The thread's unconsumed items, oldest first. Stored bytes are parsed again (a boundary). */
export function pendingItems(
  db: SqliteDriver,
  tenant: string,
  threadId: string,
): Result<readonly InboxItem[], LogError> {
  const rows = parseRows(
    Row,
    db.all(
      `SELECT inbox_id, channel, installation_id, delivery_id, received_at, item FROM inbox
        WHERE tenant_id = ? AND thread_id = ? AND consumed_seq IS NULL ORDER BY inbox_id`,
      [tenant, threadId],
    ),
  );
  if (!rows.ok) return rows;
  const items: InboxItem[] = [];
  for (const { item, ...row } of rows.value) {
    const parsed = Inbound.safeParse(
      JSON.parse(new TextDecoder().decode(item)),
    );
    if (!parsed.success || parsed.data.kind === "ignore")
      return {
        ok: false,
        error: {
          code: "log_corrupt",
          message: "an inbox row fails its schema",
        },
      };
    items.push({ ...row, item: parsed.data });
  }
  return { ok: true, value: items };
}

/** Marks an item consumed; `seq` is the event that recorded it, 0 when none was appended. */
export function consumed(db: SqliteDriver, inboxId: number, seq: number): void {
  db.run(
    "UPDATE inbox SET consumed_seq = ? WHERE inbox_id = ? AND consumed_seq IS NULL",
    [seq, inboxId],
  );
}

/** The conversation a thread was created for, if it is a channel thread. */
export function conversationOf(
  db: SqliteDriver,
  tenant: string,
  threadId: string,
): Conversation | undefined {
  const rows = parseRows(
    z.strictObject({
      channel: z.string(),
      installation_id: z.string(),
      address: z.string(),
    }),
    db.all(
      `SELECT channel, installation_id, address FROM channel_threads
        WHERE tenant_id = ? AND thread_id = ?`,
      [tenant, threadId],
    ),
  );
  const row = rows.ok ? rows.value[0] : undefined;
  return row === undefined
    ? undefined
    : {
        tenant,
        channel: row.channel,
        installation: row.installation_id,
        address: row.address,
      };
}
