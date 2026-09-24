import { describe, expect, test } from "bun:test";
import { z } from "zod";
import type { EventOf } from "../../src/fold/state";
import { BranchId, EndedResult, ThreadId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { ok } from "../../src/result";
import type { EventDraft, Writer } from "../../src/store";
import { Batch } from "../../src/team/batch";
import { type ConsumeContext, consume } from "../../src/team/consume";
import { materialize } from "../../src/team/materialize";
import { send, start } from "../../src/team/ops";
import { turnProvenance } from "../../src/team/provenance";
import { type Settlement, settle } from "../../src/team/settle";
import type { Fixture } from "../store/helpers";
import { unwrap } from "../store/helpers";
import { assertTeamReplays } from "./kit";
import {
  changes,
  DOC,
  rows,
  seeded,
  TEAM,
  type Vector,
  vectorMint,
  worldLogs,
} from "./vectors";

// The op vectors this build runs (spec/conformance/vectors/team-ops.json): each op on its world
// through this runtime's own store ops reaches the reference's outcome, appends the same event
// types per log and makes the same row changes; the team then replays.

/** Ops and vectors other lanes build: the operator's side (21F), asks, waits, monitors and
 * cancels (21E), and the control mail only they consume. */
const LATER: ReadonlySet<string> = new Set([
  "ask",
  "reply",
  "wait",
  "monitor",
  "cancel",
  "deadline",
]);
const CONTROL_21E: ReadonlySet<string> = new Set([
  "ask-reply-closes-ask",
  "ask-bounce-closes-member-ended",
  "consume-member-parked-parks-lead",
  "consume-member-parked-after-settle",
  "cancel-applied-running-member",
  "cancel-applied-parked-asker",
  "cancel-applied-asker-with-pending-reply",
  "cancel-applied-waiter",
  "cancel-applied-waiter-counts-committed-settlement-cancel-first",
  "cancel-applied-waiter-counts-committed-settlement-settlement-first",
  "cancel-stops-new-work",
]);

const runs = (v: Vector): boolean =>
  v.by !== "team" && !LATER.has(v.op) && !CONTROL_21E.has(v.name);

const Args = {
  start: z.object({ agent: z.string(), task: z.string() }),
  send: z.object({ to: z.string(), text: z.string() }),
};
const put = (): never => {
  throw new Error("the vectors carry no body above the inline cap");
};

function writerOf(fx: Fixture, v: Vector): Writer {
  const log = worldLogs(v)[v.by];
  if (log === undefined) throw new Error(`no log ${v.by}`);
  return unwrap(fx.store.acquire(BranchId.parse(log.branch_id), "vectors"));
}

/** Appends what `decide` adds to a batch under the op's writer, at the vector's clock. */
function decided(w: Writer, decide: (ctx: ConsumeContext) => void): void {
  const header = w.chain.segments.at(-1)?.header;
  if (header === undefined) throw new Error("a writer has a header");
  const appended = w.appendDecided((tx) => {
    const batch = new Batch(tx.chain.fold.seq, tx.now, vectorMint);
    decide({
      db: tx.db,
      chain: tx.chain,
      batch,
      threadId: header.thread_id,
      branchId: header.branch_id,
    });
    return ok(batch.drafts);
  });
  if (!("ok" in appended) || !appended.ok) throw new Error("append failed");
}

/** A model call's outcome: its one tool_result, as the value it records. */
function callOp(fx: Fixture, v: Vector): unknown {
  const w = writerOf(fx, v);
  const callId = z.string().parse(v.input["call_id"]);
  const call = knownEvents(w.chain).find(
    (e): e is EventOf<"tool_call"> =>
      e.type === "tool_call" && e.data.call_id === callId,
  );
  if (call === undefined) throw new Error(`no call ${callId}`);
  decided(w, (ctx) => {
    const c = { ...ctx, call, put };
    if (v.op === "send") send(c, Args.send.parse(v.input["args"]), limits(v));
    else
      start(c, Args.start.parse(v.input["args"]), {
        agents: agents(),
        limits: limits(v),
        headroom: () => v.given.headroom ?? true,
        threadId: ThreadId.parse(v.input["thread_id"]),
      });
  });
  const result = knownEvents(w.chain).findLast((e) => e.type === "tool_result");
  if (result?.type !== "tool_result") throw new Error("no result");
  return JSON.parse(result.data.preview);
}

const limits = (v: Vector) => ({
  concurrent: v.given.concurrent ?? 4,
  mailbox: v.given.mailbox ?? 100,
});

/** The agents the worlds' team lists, each with the config_hash its member logs pin. */
function agents(): ReadonlyMap<string, { readonly configHash: string }> {
  const out = new Map<string, { readonly configHash: string }>();
  for (const e of Object.values(DOC.events)) {
    const data = z
      .object({ agent_name: z.string(), config_hash: z.string() })
      .safeParse(e["data"]);
    if (
      e["type"] === "thread_started" &&
      data.success &&
      data.data.agent_name !== "lead"
    )
      out.set(data.data.agent_name, { configHash: data.data.config_hash });
  }
  return out;
}

function consumeOp(fx: Fixture, v: Vector): unknown {
  const w = writerOf(fx, v);
  let out: unknown;
  decided(w, (ctx) => {
    const got = consume(ctx);
    out =
      got.status === "nothing_pending"
        ? got
        : { status: got.status, mail_ids: got.mailIds };
  });
  return out;
}

function turnEnd(v: Vector): EventDraft {
  const reason = z
    .enum([
      "end_turn",
      "error",
      "model_unavailable",
      "cancelled",
      "budget_exhausted",
    ])
    .parse(v.input["reason"] ?? "end_turn");
  return {
    type: "turn_completed",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: { reason },
  };
}

/** A member's own settling append: the turn's end, then its settlement. */
function settleOp(fx: Fixture, v: Vector): unknown {
  const w = writerOf(fx, v);
  const events = knownEvents(w.chain);
  const idle = v.op === "idle";
  const how: Settlement = idle
    ? { status: "completed", output: lastText(events) }
    : endedOf(v.input["result"]);
  decided(w, (ctx) => {
    ctx.batch.add(turnEnd(v));
    const provenance = turnProvenance(ctx.db, ctx.chain);
    if (provenance === undefined) throw new Error("no turn");
    settle({ ...ctx, provenance, put }, how);
  });
  if (!idle) return { status: "ended" };
  const settled = knownEvents(w.chain).findLast(
    (e) => e.type === "member_idle",
  );
  return settled?.type === "member_idle"
    ? { status: "idle", result: settled.data.result }
    : undefined;
}

/** An end's outcome, as the vector gives it: an ended result without its member. */
function endedOf(raw: unknown): Settlement {
  const member = { tenant: "acme", team: TEAM, name: "lead", generation: 1 };
  const { member: _member, ...how } = EndedResult.parse({
    member,
    ...z.record(z.string(), z.json()).parse(raw),
  });
  return how;
}

function lastText(events: readonly { type: string }[]): string {
  const e = events.findLast((x) => x.type === "model_response");
  if (e === undefined || !("data" in e)) return "";
  const parsed = z
    .object({
      content: z.array(
        z.object({ type: z.string(), text: z.string().optional() }),
      ),
    })
    .parse(e.data);
  return parsed.content
    .map((p) => (p.type === "text" ? (p.text ?? "") : ""))
    .join("");
}

async function materializeOp(fx: Fixture, v: Vector): Promise<unknown> {
  const rebind = z
    .enum(["ok", "pin_unavailable", "pin_mismatch"])
    .parse(v.input["rebind"]);
  const got = unwrap(
    await materialize(fx.store, TEAM, z.string().parse(v.input["member"]), {
      artifacts: fx.artifacts,
      rebind: async () => ({ status: rebind }),
      holder: "vectors",
      ttlMs: 30_000,
      mint: vectorMint,
      branchId: BranchId.parse(v.input["branch_id"]),
    }),
  );
  return got.status === "rebind_failed"
    ? { status: got.status, code: got.code }
    : { status: got.status };
}

async function run(fx: Fixture, v: Vector): Promise<unknown> {
  switch (v.op) {
    case "send":
    case "start":
      return callOp(fx, v);
    case "consume":
      return consumeOp(fx, v);
    case "idle":
    case "end":
      return settleOp(fx, v);
    case "materialize":
      return materializeOp(fx, v);
    default:
      throw new Error(`op ${v.op} is not this build's`);
  }
}

/** The event types appended per log label since `heads`. */
function appended(
  fx: Fixture,
  v: Vector,
  heads: ReadonlyMap<string, number>,
): Readonly<Record<string, readonly string[]>> {
  const out: Record<string, string[]> = {};
  const labels = {
    ...Object.fromEntries(
      Object.entries(worldLogs(v)).map(([k, l]) => [k, l.branch_id]),
    ),
  };
  if (v.op === "materialize")
    labels[z.string().parse(v.input["label"])] = z
      .string()
      .parse(v.input["branch_id"]);
  for (const [label, branch] of Object.entries(labels)) {
    const read = fx.store.read(BranchId.parse(branch));
    if (!read.ok) continue;
    const types = knownEvents(read.value)
      .filter((e) => e.seq > (heads.get(label) ?? 0))
      .map((e) => e.type);
    if (types.length > 0) out[label] = types;
  }
  return out;
}

describe("team op vectors, run by this runtime", () => {
  const mine = DOC.vectors.filter(runs);

  test("cover start, send, consume, materialize, idle and end", () => {
    expect(new Set(mine.map((v) => v.op))).toEqual(
      new Set(["start", "send", "consume", "materialize", "idle", "end"]),
    );
  });

  for (const v of mine)
    test(v.name, async () => {
      const fx = seeded(v);
      const before = rows(fx);
      const heads = new Map(
        Object.entries(worldLogs(v)).map(([label, log]) => [
          label,
          unwrap(fx.store.read(BranchId.parse(log.branch_id))).events.length,
        ]),
      );
      expect(await run(fx, v)).toEqual(v.expect.outcome);
      expect(appended(fx, v, heads)).toEqual(v.expect.appended);
      expect(changes(before, rows(fx))).toEqual(v.expect.rows);
      assertTeamReplays(fx.store, TEAM);
    });
});
