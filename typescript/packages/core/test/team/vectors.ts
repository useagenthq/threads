import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { sha256Hex } from "../../src/hash";
import {
  BranchId,
  canonicalize,
  type TeamId,
  TeamId as TeamIdSchema,
} from "../../src/log";
import type { StoreDriver } from "../../src/store";
import { IMPL } from "../../src/store/writer";
import type { Template } from "../../src/team/dynamic";
import { rebuildTeamIndex } from "../../src/team/rebuild";
import { VERSION } from "../../src/version";
import { type Fixture, fixture, unwrap } from "../store/helpers";
import { query, storeLogs, teamIndexRows, verified } from "./kit";

// The op vectors (spec/conformance/vectors/team-ops.json) as a runtime reads them: a world's logs
// seeded under this implementation's own headers and chained, its index rebuilt, and the rows it
// holds as the vectors list them.

const FILE = join(
  import.meta.dir,
  "../../../../../spec/conformance/vectors/team-ops.json",
);
type Json = Readonly<Record<string, unknown>>;
type WorldLog = {
  readonly thread_id: string;
  readonly branch_id: string;
  readonly events: readonly string[];
};
export type Vector = {
  readonly name: string;
  readonly op: string;
  readonly by: string;
  readonly now: number;
  readonly input: Json;
  readonly given: {
    readonly world: string;
    readonly mailbox?: number | undefined;
    readonly concurrent?: number | undefined;
    readonly headroom?: boolean | undefined;
    readonly templates?: Readonly<Record<string, Template>> | undefined;
  };
  readonly expect: {
    readonly outcome: unknown;
    readonly appended: Readonly<Record<string, readonly string[]>>;
    readonly rows: Readonly<
      Record<string, Readonly<Record<string, readonly unknown[]>>>
    >;
  };
};
type Doc = {
  readonly vectors: readonly Vector[];
  readonly worlds: Readonly<
    Record<string, { readonly logs: Readonly<Record<string, WorldLog>> }>
  >;
  readonly events: Readonly<Record<string, Json>>;
};

const Json: z.ZodType<Json> = z.record(z.string(), z.json());
const LogRef: z.ZodType<WorldLog> = z.strictObject({
  thread_id: z.string(),
  branch_id: z.string(),
  events: z.array(z.string()),
});
const Doc: z.ZodType<Doc> = z.object({
  vectors: z.array(
    z.object({
      name: z.string(),
      op: z.string(),
      by: z.string(),
      now: z.int(),
      input: Json,
      given: z.object({
        world: z.string(),
        mailbox: z.int().optional(),
        concurrent: z.int().optional(),
        headroom: z.boolean().optional(),
        templates: z
          .record(
            z.string(),
            z.object({
              tools: z.array(z.string()),
              models: z.array(z.string()),
            }),
          )
          .optional(),
      }),
      expect: z.object({
        outcome: z.json(),
        appended: z.record(z.string(), z.array(z.string())),
        rows: z.record(z.string(), z.record(z.string(), z.array(z.json()))),
      }),
    }),
  ),
  worlds: z.record(
    z.string(),
    z.object({ logs: z.record(z.string(), LogRef) }),
  ),
  events: z.record(z.string(), Json),
});

export const DOC: Doc = Doc.parse(JSON.parse(readFileSync(FILE, "utf8")));

/** Every table a vector's row changes name, with its primary key. */
export const KEYS: Readonly<Record<string, readonly string[]>> = {
  teams: ["team_id"],
  team_members: ["team_id", "name", "generation"],
  mail: ["mail_id"],
  asks: ["ask_id"],
  monitors: ["monitor_id"],
  operator_receipts: ["tenant_id", "team_id", "op", "idempotency_key"],
  pending_wakes: ["branch_id", "child_thread_id"],
};

const utf8 = new TextEncoder();
const line = (value: unknown): string =>
  unwrap(canonicalize(z.json().parse(value)));

/** One world log's export: this implementation's header, the events chained, the head. */
function exported(ref: WorldLog): Uint8Array {
  const header = line({
    branch_id: ref.branch_id,
    created_at: 1_790_000_000_000,
    format: "threads.log",
    format_version: 1,
    thread_id: ref.thread_id,
    writer: { impl: IMPL, version: VERSION },
  });
  const lines = [header];
  let prev = sha256Hex(utf8.encode(header));
  for (const id of ref.events) {
    const event = DOC.events[id];
    if (event === undefined) throw new Error(`no world event ${id}`);
    const text = line({ ...event, prev_hash: prev });
    lines.push(text);
    prev = sha256Hex(utf8.encode(text));
  }
  const last = DOC.events[ref.events.at(-1) ?? ""];
  lines.push(
    line({
      format: "threads.head",
      format_version: 1,
      branch_id: ref.branch_id,
      seq: last?.["seq"] ?? 0,
      hash: prev,
    }),
  );
  return utf8.encode(`${lines.join("\n")}\n`);
}

/** The world's logs, by label. */
export function worldLogs(v: Vector): Readonly<Record<string, WorldLog>> {
  const world = DOC.worlds[v.given.world];
  if (world === undefined) throw new Error(`no world ${v.given.world}`);
  return world.logs;
}

/** The team the vectors' worlds share. */
export const TEAM: TeamId = TeamIdSchema.parse(
  "0192c000-0000-7000-8000-000000000001",
);

/**
 * A store holding the vector's world at its clock, the team index rebuilt from its logs, and every
 * member config the vectors pin stored under its config_hash (as start writes it).
 */
export async function seeded(v: Vector, db?: StoreDriver): Promise<Fixture> {
  const fx = await fixture("acme", db);
  fx.clock.now = v.now;
  const logs = Object.values(worldLogs(v)).map((ref) =>
    verified(exported(ref)),
  );
  await storeLogs(fx.store, logs);
  unwrap(await rebuildTeamIndex(fx.store, TEAM));
  for (const config of configs()) await fx.artifacts.put(config);
  return fx;
}

/** The pinned config of every thread_started among the vectors' events. */
function configs(): readonly Uint8Array[] {
  return Object.values(DOC.events).flatMap((e) => {
    if (e["type"] !== "thread_started") return [];
    const {
      config_hash: hash,
      parent: _p,
      team: _t,
      ...cfg
    } = Json.parse(e["data"]);
    const bytes = utf8.encode(line(cfg));
    return sha256Hex(bytes) === hash ? [bytes] : [];
  });
}

/** The index rows, per table, as the vectors list them. */
export async function rows(fx: Fixture): Promise<Record<string, unknown[]>> {
  const branches = (await query(fx.db, "SELECT branch_id FROM branches")).map(
    (r) => z.object({ branch_id: BranchId }).parse(r).branch_id,
  );
  const { team_feed: _feed, ...tables } = await teamIndexRows(
    fx.db,
    [TEAM],
    branches,
  );
  return tables;
}

/** Per table, the rows inserted, updated (as they are now) and the keys deleted. */
export function changes(
  before: Record<string, unknown[]>,
  after: Record<string, unknown[]>,
): Readonly<Record<string, Readonly<Record<string, readonly unknown[]>>>> {
  const out: Record<string, Record<string, unknown[]>> = {};
  for (const [table, cols] of Object.entries(KEYS)) {
    const key = (row: unknown): string => {
      const r = Json.parse(row);
      return line(Object.fromEntries(cols.map((c) => [c, r[c] ?? null])));
    };
    const old = new Map((before[table] ?? []).map((r) => [key(r), r]));
    const now = new Map((after[table] ?? []).map((r) => [key(r), r]));
    const change: Record<string, unknown[]> = {};
    const inserted = [...now].filter(([k]) => !old.has(k)).map(([, r]) => r);
    const updated = [...now]
      .filter(([k, r]) => old.has(k) && line(old.get(k)) !== line(r))
      .map(([, r]) => r);
    const deleted = [...old]
      .filter(([k]) => !now.has(k))
      .map(([, r]) =>
        Object.fromEntries(cols.map((c) => [c, Json.parse(r)[c] ?? null])),
      );
    if (inserted.length > 0) change["insert"] = inserted;
    if (updated.length > 0) change["update"] = updated;
    if (deleted.length > 0) change["delete"] = deleted;
    if (Object.keys(change).length > 0) out[table] = change;
  }
  return out;
}

/** The reference's event ids: `eid(seq, branch)` for a team branch. */
export const vectorMint = (seq: number): string =>
  `0192e001-0000-7000-8000-${seq.toString(16).padStart(12, "0")}`;
