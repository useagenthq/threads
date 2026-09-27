import { z } from "zod";
import type { EventOf } from "../fold/state";
import { BranchId, ThreadStartedFields } from "../log";
import { knownEvents } from "../reduce";
import { err, ok, type Result } from "../result";
import type { LogStore } from "../store";
import { READ_ONLY, type Tx } from "../store/driver";
import { uuidv7 } from "../store/encode";
import { type LogError, logError } from "../verify/error";
import { Batch, MINT } from "./batch";
import { LINE_ZERO } from "./line-zero";
// Types only, so nothing of materialize.ts is imported at run time: it imports this module.
import type { Materialized, MaterializeOptions, Rebind } from "./materialize";
import { type MemberRow, memberNamed, teamRow } from "./rows";
import { settle } from "./settle";

// Materializing a host member (spec/schema/README.md, "Teams Phase 2"): its root thread opens with
// thread_started{host_member} alone. A host member has no task, so its open records no user_input
// and opens no turn; the row's branch is set and it becomes idle, ready for the first caller's
// mail. A failed rebind ends it in the same append, with no turn to close.
// Reference: spec/tools/fixtures/ref_host.py.

const Pinned = z.object(ThreadStartedFields.pick(LINE_ZERO).shape);

/** A starting host member: its row, its member_started in the host team log, and its line 0. */
type Start = {
  readonly row: MemberRow;
  readonly started: EventOf<"member_started">;
  readonly pinned: z.infer<typeof Pinned>;
};

/** Opens the host member's root thread, or reports why nothing was opened. */
export async function materializeHost(
  store: LogStore,
  team: string,
  row: MemberRow,
  o: MaterializeOptions,
): Promise<Result<Materialized, LogError>> {
  const found = await start(store, team, row, o);
  if (!found.ok) return found;
  if (found.value === undefined) return ok({ status: "not_starting" });
  const s = found.value;
  const rebind = await o.rebind(
    s.started.data.agent,
    s.started.data.config_hash,
    undefined,
  );
  const branchId = o.branchId ?? BranchId.parse(uuidv7(store.now()));
  const opened = await store.openBranchChecked(async (tx, now) => {
    const live = await memberNamed(tx, team, row.name);
    if (live?.state !== "starting" || live.generation !== row.generation)
      return ok(undefined);
    const batch = new Batch(0, now, o.mint ?? MINT);
    await firstEvents(tx, batch, s, rebind, branchId);
    return ok({
      threadId: s.started.data.thread_id,
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
 * thread_started{host_member} and, after a failed rebind, the end of the member. There is no task
 * and so no turn: a failed rebind writes no turn_completed, and its end belongs to no request.
 */
async function firstEvents(
  tx: Tx,
  batch: Batch,
  s: Start,
  rebind: Rebind,
  branchId: string,
): Promise<void> {
  const {
    member,
    config_hash: configHash,
    thread_id: threadId,
  } = s.started.data;
  batch.add({
    type: "thread_started",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      ...s.pinned,
      config_hash: configHash,
      host_member: {
        team: member.team,
        name: member.name,
        generation: member.generation,
      },
    },
  });
  if (rebind.status === "ok") return;
  const code = rebind.status;
  await settle(
    {
      tx,
      batch,
      threadId,
      branchId,
      provenance: undefined,
      put: async () => {
        throw new Error("a failed rebind's result has no text");
      },
    },
    { status: "failed", error: { code, message: `rebind failed: ${code}` } },
  );
}

/** The member_started the host team log holds for this row, and its pinned line 0. */
async function start(
  store: LogStore,
  team: string,
  row: MemberRow,
  o: MaterializeOptions,
): Promise<Result<Start | undefined, LogError>> {
  if (row.state !== "starting") return ok(undefined);
  const teams = await store.driver.transaction(
    (tx) => teamRow(tx, team),
    READ_ONLY,
  );
  if (teams === undefined) return ok(undefined);
  const log = await store.read(teams.team_log_branch_id);
  if (!log.ok) return log;
  const started = knownEvents(log.value).find(
    (e): e is EventOf<"member_started"> =>
      e.type === "member_started" &&
      e.data.member.name === row.name &&
      e.data.member.generation === row.generation,
  );
  if (started === undefined)
    return err(logError("log_corrupt", `no member_started for ${row.name}`));
  const config = await o.artifacts.get(started.data.config_hash);
  if (!config.ok) return config;
  const pinned = Pinned.parse(
    JSON.parse(new TextDecoder().decode(config.value)),
  );
  return ok({ row, started, pinned });
}
