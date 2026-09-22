import { BranchId, ThreadId } from "../../src/log";
import type { Result } from "../../src/result";
import {
  type ArtifactStore,
  type EventDraft,
  LOCAL_TENANT,
  LogStore,
  memoryArtifacts,
  type SqliteDriver,
} from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";

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
  readonly db: SqliteDriver;
  readonly store: LogStore;
  readonly clock: { now: number };
};

/** An in-memory store on bun:sqlite with an injected clock, bound to one tenant. */
export function fixture(
  tenantId: string = LOCAL_TENANT,
  db: SqliteDriver = openBunSqlite(":memory:"),
): Fixture {
  const clock = { now: T0 };
  const now = (): number => clock.now;
  const artifacts = memoryArtifacts();
  const store = unwrap(LogStore.open(db, now, artifacts, tenantId));
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

export function count(db: SqliteDriver, branch: string): unknown {
  return db.all("SELECT count(*) AS n FROM events WHERE branch_id = ?", [
    branch,
  ])[0];
}
