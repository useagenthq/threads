import type { EventOf } from "../../fold/state";
import type { Json, Principal, TeamId } from "../../log";
import { redactSecrets } from "../../redact/text";
import { knownEvents } from "../../reduce";
import type { LogStore } from "../../store";
import type { ArtifactStore } from "../../store/artifacts";
import { reading } from "../../store/driver";
import { uuidv7 } from "../../store/encode";
import type { DecideTx } from "../../store/writer";
import type { Batch, Mint } from "../../team/batch";
import { type CloseContext, readerOf } from "../../team/close";
import {
  type OperatorOp,
  openOperator,
  type Recorded,
} from "../../team/operator";
import type { TeamLimits } from "../../team/ops";
import type { Request } from "../../team/request";
import { memberRows, type TeamRow, teamRow } from "../../team/rows";
import type { DeferTools } from "../defer";
import type { MemberEntry } from "../registry";
import type { Store } from "../sqlite";
import type { TeamRef } from "./handle-types";
import { type BUSY, onTeamLog } from "./team-log";

// One operator request of the Team handle (design §4.4): its key looked up under the team log's
// writer, then decided by the op in the same append. Shared by every Team method that writes.

export type HandleEnv = {
  readonly log: LogStore;
  readonly artifacts: ArtifactStore;
  readonly ref: TeamRef;
  readonly principal: Principal;
  /** The lead that ran, as this process defines it: the agents start resolves. */
  readonly lead: MemberEntry;
  /** The store the team's members run on, when the handle drives them. */
  readonly store: Store;
  readonly busyBoundMs?: number;
  readonly mint?: Mint;
};

const utf8 = new TextEncoder();

/**
 * One operator request under the team-log writer: its key looked up, then `decide` with the op,
 * given the request and the append as a close (for a wait that finishes in it). Returns what the
 * op or the key's replay recorded, or busy.
 */
export async function operator(
  env: HandleEnv,
  op: OperatorOp,
  body: Readonly<Record<string, Json | undefined>>,
  idempotencyKey: string | undefined,
  decide: (
    req: Request,
    team: TeamRow,
    close: CloseContext,
  ) => Promise<Recorded>,
): Promise<Recorded | typeof BUSY> {
  const team = await reading(env.log.driver, (tx) => teamRow(tx, env.ref.id));
  if (team === undefined) throw new Error(`no team ${env.ref.id}`);
  const requestId = uuidv7(env.log.now());
  return await onTeamLog(
    env.log,
    team.team_log_branch_id,
    async (tx: DecideTx, batch: Batch) => {
      // Read in the append: the team may have closed since the handle looked.
      const now = (await teamRow(tx.tx, env.ref.id)) ?? team;
      const header = tx.chain.segments[0]?.header;
      if (header === undefined) throw new Error("a team log has a header");
      const read = readerOf(env.artifacts);
      const opened = await openOperator(
        {
          tx: tx.tx,
          chain: tx.chain,
          batch,
          put: putText(env),
          read,
          team: now,
        },
        {
          requestId,
          op,
          principal: env.principal,
          body: present(body),
          idempotencyKey,
        },
      );
      if (opened.kind === "recorded") return opened.outcome;
      const close = {
        tx: tx.tx,
        chain: tx.chain,
        batch,
        threadId: header.thread_id,
        branchId: header.branch_id,
        read,
      };
      return decide(opened.request, now, close);
    },
    {
      ...(env.busyBoundMs === undefined
        ? {}
        : { busyBoundMs: env.busyBoundMs }),
      ...(env.mint === undefined ? {} : { mint: env.mint }),
    },
  );
}

/** A body's parameters as api.json names them, an omitted option left out. */
function present(
  body: Readonly<Record<string, Json | undefined>>,
): Readonly<Record<string, Json>> {
  return Object.fromEntries(
    Object.entries(body).flatMap(([k, v]) => (v === undefined ? [] : [[k, v]])),
  );
}

/** A text above the inline cap, stored before the append that names it (redacted, C5). */
function putText(env: HandleEnv): Request["put"] {
  return async (text) => {
    const bytes = utf8.encode(redactSecrets(text));
    return {
      sha256: await env.artifacts.put(bytes),
      bytes: bytes.length,
      media_type: "text/plain",
    };
  };
}

export const limitsOf = (env: HandleEnv): TeamLimits => env.lead.teamLimits;

/** The lead as a member's parent, and the defer_tools its members inherit. */
export async function leadOf(
  log: LogStore,
  team: TeamId,
): Promise<{
  readonly parent: NonNullable<EventOf<"thread_started">["data"]["parent"]>;
  readonly deferTools: DeferTools | undefined;
}> {
  const row = (await reading(log.driver, (tx) => memberRows(tx, team))).find(
    (r) => r.role === "lead",
  );
  const read =
    row?.branch_id === undefined || row.branch_id === null
      ? undefined
      : await log.read(row.branch_id);
  if (row === undefined || read === undefined || !read.ok)
    throw new Error(`team ${team} has no readable lead log`);
  const started = knownEvents(read.value).find(
    (e) => e.type === "thread_started",
  );
  if (started?.type !== "thread_started" || row.branch_id === null)
    throw new Error("a lead log starts with thread_started");
  return {
    parent: {
      thread_id: row.thread_id,
      branch_id: row.branch_id,
      event_id: started.event_id,
      relation: "team_member",
    },
    deferTools: started.data.policy?.context?.defer_tools,
  };
}

/** The codes the team log records for an operator's key (its refusals, never a replay). */
export const KEYED = [
  "idempotency_key_reused",
  "idempotency_key_principal_mismatch",
] as const;

export const isIn = <C extends string>(
  codes: readonly C[],
  code: string,
): code is C => codes.some((c) => c === code);
