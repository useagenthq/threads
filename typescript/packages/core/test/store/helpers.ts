import { BranchId, ThreadId } from "../../src/log";
import type { Result } from "../../src/result";
import {
  type ArtifactStore,
  type EventDraft,
  LOCAL_TENANT,
  LogStore,
  memoryArtifacts,
  type StoreDriver,
} from "../../src/store";
import { freshDriver } from "./engine";

export const THREAD: ThreadId = ThreadId.parse(
  "0192a000-0000-7000-8000-000000000001",
);
export const ROOT: BranchId = BranchId.parse(
  "0192b000-0000-7000-8000-000000000001",
);
export const CHILD: BranchId = BranchId.parse(
  "0192b000-0000-7000-8000-000000000002",
);
export const T0 = 1_790_000_000_000;

export type Fixture = {
  readonly artifacts: ArtifactStore;
  readonly db: StoreDriver;
  readonly store: LogStore;
  readonly clock: { now: number };
};

/**
 * A fresh store of the engine under test (engine.ts: in-memory bun:sqlite by default) with an
 * injected clock, bound to one tenant. A given `db` is used as it is, with in-memory artifacts.
 */
export async function fixture(
  tenantId: string = LOCAL_TENANT,
  given?: StoreDriver,
): Promise<Fixture> {
  const clock = { now: T0 };
  const now = (): number => clock.now;
  const { db, artifacts } =
    given === undefined
      ? await freshDriver()
      : { db: given, artifacts: memoryArtifacts() };
  const store = unwrap(await LogStore.open(db, now, artifacts, tenantId));
  return { db, store, clock, artifacts };
}

/** The value of an ok result; a test fails loudly on an error. */
export function unwrap<T, E>(result: Result<T, E>): T {
  if (!result.ok)
    throw new Error(`expected ok: ${JSON.stringify(result.error)}`);
  return result.value;
}

/** An error code, or "ok": what most assertions compare. */
export function code<T, E extends { readonly code: string }>(
  result: Result<T, E>,
): string {
  return result.ok ? "ok" : result.error.code;
}

const ALICE = { issuer: "api", tenant: "acme", subject: "alice" };

export const started: EventDraft = {
  type: "thread_started",
  type_version: 1,
  critical: true,
  actor: { kind: "host" },
  data: {
    agent_name: "demo",
    config_hash: "a".repeat(64),
    model: { provider: "scripted", name: "scripted-1" },
    model_params: { max_tokens: 1024 },
    adapter: { name: "scripted", version: "1", settings: {} },
    instructions: "You are a helpful agent.",
    tools: [],
  },
};

export function userInput(text: string): EventDraft {
  return {
    type: "user_input",
    type_version: 1,
    critical: true,
    actor: { kind: "user", principal: ALICE },
    data: { source: "api", text },
  };
}

export const turnCompleted: EventDraft = {
  type: "turn_completed",
  type_version: 1,
  critical: true,
  actor: { kind: "host" },
  data: { reason: "end_turn" },
};

export function snapshot(expiresAt: number | null): EventDraft {
  return {
    type: "snapshot",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      snapshot_id: "snap_01",
      provider: "fake",
      sandbox_id: "sbx_parent_01",
      capture_class: "filesystem",
      expires_at: expiresAt,
      manifest_hash: "b".repeat(64),
      quiesced: { frozen: [], stopped: [], excluded: [] },
    },
  };
}

type Param = string | number | Uint8Array | null;

/** Test-only raw SQL: each call is a transaction of its own. */
export function rows(
  db: StoreDriver,
  sql: string,
  params: readonly Param[] = [],
): Promise<readonly unknown[]> {
  return db.transaction((tx) => tx.all(sql, params));
}

export function run(
  db: StoreDriver,
  sql: string,
  params: readonly Param[] = [],
): Promise<number> {
  return db.transaction((tx) => tx.run(sql, params));
}

export async function count(db: StoreDriver, branch: string): Promise<unknown> {
  const rows = await db.transaction((tx) =>
    tx.all("SELECT count(*) AS n FROM events WHERE branch_id = ?", [branch]),
  );
  return rows[0];
}
