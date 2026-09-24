import { z } from "zod";
import type { EventOf } from "../fold/state";
import {
  BranchId,
  type MailEnvelope,
  type TeamSettings,
  ThreadStartedData,
} from "../log";
import { knownEvents } from "../reduce";
import { err, ok, type Result } from "../result";
import type { LogStore, Writer } from "../store";
import type { ArtifactStore } from "../store/artifacts";
import type { SqliteDriver } from "../store/driver";
import { uuidv7 } from "../store/encode";
import { type LogError, logError } from "../verify/error";
import { Batch, MINT, type Mint } from "./batch";
import { type MemberRow, memberNamed, pendingTo, teamRow } from "./rows";
import { settle } from "./settle";

// Materialize (spec/schema/README.md, "Teams"; design §4.10): the team worker's first consume of
// a starting member. Its prework, outside any transaction, rebinds the member's definition by
// name; then one write transaction re-reads the row (gone or no longer starting commits nothing)
// and opens the member's branch with thread_started and its task as user_input. A failed rebind
// is one complete end-of-member append instead, and no model request is ever made for it.
// Reference: spec/tools/fixtures/ops_start.py.

/** What rebinding the member's definition by name found. */
export type Rebind =
  | {
      readonly status: "ok";
      /** A nested lead's own team, which its first append opens. */
      readonly team?: z.input<typeof TeamSettings>;
    }
  | { readonly status: "pin_unavailable" | "pin_mismatch" };

export type Materialized =
  | { readonly status: "materialized"; readonly writer: Writer }
  | { readonly status: "not_starting" }
  | {
      readonly status: "rebind_failed";
      readonly code: "pin_unavailable" | "pin_mismatch";
    };

export type MaterializeOptions = {
  readonly artifacts: ArtifactStore;
  readonly rebind: (agent: string, configHash: string) => Promise<Rebind>;
  /** The lease holder the member's first writer runs under. */
  readonly holder: string;
  readonly ttlMs: number;
  readonly mint?: Mint;
  /** The member's branch id; a new one by default. */
  readonly branchId?: BranchId;
};

/** A starting member: its row, its task and the member_started its starter recorded. */
type Starting = {
  readonly row: MemberRow;
  readonly task: MailEnvelope;
  readonly started: EventOf<"member_started">;
};

// thread_started takes the pinned config's line-0 fields; the rest of the config is hashed only.
const Pinned = z.object(
  ThreadStartedData.pick({
    agent_name: true,
    instructions: true,
    model: true,
    model_params: true,
    adapter: true,
    tools: true,
    policy: true,
    sandbox_provider: true,
  }).shape,
);

/** Materializes the member `name` of `team`, or reports why nothing was opened. */
export async function materialize(
  store: LogStore,
  team: string,
  name: string,
  o: MaterializeOptions,
): Promise<Result<Materialized, LogError>> {
  const found = starting(store, team, name);
  if (!found.ok) return found;
  if (found.value === undefined) return ok({ status: "not_starting" });
  const { started } = found.value;
  const rebind = await o.rebind(started.data.agent, started.data.config_hash);
  const config = o.artifacts.get(started.data.config_hash);
  if (!config.ok) return config;
  const pinned = Pinned.parse(
    JSON.parse(new TextDecoder().decode(config.value)),
  );
  const branchId = o.branchId ?? BranchId.parse(uuidv7(store.now()));
  const opened = store.openBranchChecked((db, now) => {
    const row = memberNamed(db, team, name);
    if (
      row?.state !== "starting" ||
      row.generation !== found.value?.row.generation
    )
      return ok(undefined);
    const batch = new Batch(0, now, o.mint ?? MINT);
    firstEvents(db, batch, { ...found.value, row }, pinned, rebind, branchId);
    return ok({
      threadId: started.data.thread_id,
      branchId,
      lease: {
        holderId: o.holder,
        ttlMs: rebind.status === "ok" ? o.ttlMs : 0,
      },
      drafts: batch.drafts,
    });
  });
  if (!opened.ok) return opened;
  const writer = opened.value;
  if (writer === undefined || writer === "already_open")
    return ok({ status: "not_starting" });
  return rebind.status === "ok"
    ? ok({ status: "materialized", writer })
    : ok({ status: "rebind_failed", code: rebind.status });
}

/**
 * thread_started (the pinned config, the member_started's parent) and the task's user_input,
 * whose actor is the task's principal; after a failed rebind, the turn closes before any model
 * request and the member ends failed with everything an end carries.
 */
function firstEvents(
  db: SqliteDriver,
  batch: Batch,
  s: Starting,
  pinned: z.infer<typeof Pinned>,
  rebind: Rebind,
  branchId: string,
): void {
  const { started, task } = s;
  // start writes a task's body inline.
  const text = task.body?.text;
  if (text === undefined) throw new Error(`task ${task.mail_id} has no text`);
  const nested = rebind.status === "ok" ? rebind.team : undefined;
  batch.add({
    type: "thread_started",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      ...pinned,
      config_hash: started.data.config_hash,
      parent: started.data.parent,
      ...(nested === undefined ? {} : { team: nested }),
    },
  });
  batch.add({
    type: "user_input",
    type_version: 1,
    critical: true,
    actor: { kind: "host", principal: task.provenance.principal },
    data: {
      source: "team_task",
      text,
      mail_id: task.mail_id,
    },
  });
  if (rebind.status === "ok") return;
  const code = rebind.status;
  batch.add({
    type: "turn_completed",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: { reason: "error", code },
  });
  settle(
    {
      db,
      batch,
      threadId: started.data.thread_id,
      branchId,
      provenance: task.provenance,
      put: () => {
        throw new Error("a failed rebind's result has no text");
      },
    },
    { status: "failed", error: { code, message: `rebind failed: ${code}` } },
  );
}

/** The starting row, its pending task and its member_started, read from the starter's log. */
function starting(
  store: LogStore,
  team: string,
  name: string,
): Result<Starting | undefined, LogError> {
  const db = store.driver;
  const row = memberNamed(db, team, name);
  if (row?.state !== "starting") return ok(undefined);
  const task = pendingTo(db, team, name).find((m) => m.kind === "task");
  const teams = teamRow(db, team);
  if (task === undefined || teams === undefined) return ok(undefined);
  const starter =
    "operator" in task.from
      ? ok(teams.team_log_branch_id)
      : store.mainBranch(task.causal.thread_id);
  if (!starter.ok) return starter;
  const log = store.read(starter.value);
  if (!log.ok) return log;
  const started = knownEvents(log.value).find(
    (e): e is EventOf<"member_started"> =>
      e.type === "member_started" &&
      e.data.member.name === name &&
      e.data.member.generation === row.generation,
  );
  return started === undefined
    ? err(logError("log_corrupt", `no member_started for ${name}`))
    : ok({ row, task, started });
}
