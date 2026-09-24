import { z } from "zod";
import type { EventOf, ParkAddress } from "../fold/state";
import {
  type ArtifactRef,
  type MailEnvelope,
  type MemberRef,
  ParkReason,
  StoredMemberResult,
} from "../log";
import type { ArtifactStore } from "../store/artifacts";
import type { Chain } from "../verify";
import { toolResult } from "./call";
import { received } from "./mail";
import type { AskOutcome, MemberResult, Waited, Wire } from "./results";
import { mailEnvelope, memberNamed, pendingHere } from "./rows";
import type { AppendContext } from "./settle";
import {
  askOpen,
  type Item,
  itemsOf,
  openWaits,
  parkedOn,
  settleMonitors,
  untaken,
} from "./view";

// ask.complete and a wait's finish, under the asker's or waiter's writer (spec/schema/README.md,
// "Teams"; design §4.8 and §4.12): one decision whatever triggers it (a consume, a cancel, the
// deadline step). A member also resumes its park and records the call's one tool_result.
// Reference: spec/tools/fixtures/ops_consume.py (complete) and ops_observe.py (finish).

/** Reads a {ref} body's text from the content-addressed store (sha256 and length verified). */
export type ReadText = (ref: ArtifactRef) => string;

/**
 * Text read from an artifact store. A ref the verified log names is always present, so a missing
 * or corrupt artifact is a broken store invariant: it throws.
 */
export function readerOf(artifacts: ArtifactStore): ReadText {
  return (ref) => {
    const got = artifacts.get(ref.sha256);
    if (!got.ok) throw new Error(got.error.message);
    return new TextDecoder().decode(got.value);
  };
}

/** A wait not finished yet: the waiter's call stays pending. */
export type Waiting = { readonly status: "waiting"; readonly wait_id: string };

/** A writer's append that closes asks and waits: its committed log, and where refs are read. */
export type CloseContext = AppendContext & {
  readonly chain: Chain;
  readonly read: ReadText;
};

type Outcome = EventOf<"ask_closed">["data"]["outcome"];
type Ref = z.input<typeof MemberRef>;
type Reason = z.infer<typeof ParkReason>;
type Body = NonNullable<MailEnvelope["body"]>;

const NOTICES: ReadonlySet<string> = new Set([
  "member_settled",
  "member_ended",
]);

/** The call an ask or wait belongs to: its id after the sender branch. */
export const callOf = (key: string): string => key.slice(key.indexOf(":") + 1);

function textOf(body: Body | undefined, read: ReadText): string {
  if (body?.text !== undefined) return body.text;
  if (body?.ref === undefined) throw new Error("a text body is text or a ref");
  return read(body.ref);
}

/** A stored result as the public one: a completed output is its text. */
export function publicResult(
  result: StoredMemberResult,
  read: ReadText,
): Wire<MemberResult> {
  return result.status === "completed"
    ? { ...result, output: textOf(result.output, read) }
    : result;
}

/** This writer's pending mail the batch hasn't taken. */
export function mine(ctx: CloseContext): readonly MailEnvelope[] {
  return untaken(pendingHere(ctx.db, ctx.threadId, ctx.branchId), ctx.batch);
}

const HOST = {
  type_version: 1,
  critical: true,
  actor: { kind: "host" },
} as const;

/** A member's park on `address` resumes, caused by `cause`, when it is still open. */
function resume(ctx: CloseContext, address: ParkAddress, cause: string): void {
  if (!ctx.chain.fold.team.teamLog && parkedOn(ctx.chain, ctx.batch, address))
    ctx.batch.add({
      ...HOST,
      type: "resumed",
      data: { address, cause_event_id: cause },
    });
}

/** ask_closed, and for a member asker its resumed and the ask call's one result. */
export function closeAsk(
  ctx: CloseContext,
  askId: string,
  outcome: Outcome,
  cause?: string,
): Wire<AskOutcome> {
  const closed = ctx.batch.add({
    ...HOST,
    type: "ask_closed",
    data: { ask_id: askId, outcome },
  });
  const value = askValue(ctx, askId, outcome);
  if (ctx.chain.fold.team.teamLog) return value;
  resume(ctx, { kind: "ask", id: askId }, cause ?? closed);
  ctx.batch.add(toolResult(callOf(askId), value));
  return value;
}

function askValue(
  ctx: CloseContext,
  askId: string,
  outcome: Outcome,
): Wire<AskOutcome> {
  switch (outcome.status) {
    case "answered": {
      const reply = mailEnvelope(ctx.db, outcome.reply);
      if (reply === undefined || "operator" in reply.from)
        throw new Error(`no member's reply ${outcome.reply}`);
      const text = textOf(reply.body, ctx.read);
      return { ask_id: askId, status: "answered", member: reply.from, text };
    }
    case "member_ended":
      return {
        ask_id: askId,
        status: outcome.status,
        result: publicResult(outcome.result, ctx.read),
      };
    case "timed_out":
    case "cancelled":
      return { ask_id: askId, status: outcome.status };
  }
}

/**
 * ask.complete's one decision, whatever triggers it: a pending reply, then a pending bounce naming
 * the ask, then a cancel (a member asker) or a closed team (the team log), then the deadline.
 * Undefined: nothing decides it yet.
 */
export function completeAsk(
  ctx: CloseContext,
  askId: string,
  how: { readonly cancelled: boolean; readonly due: boolean },
): Wire<AskOutcome> | undefined {
  for (const kind of ["reply", "bounce"] as const) {
    const env = mine(ctx).find((m) => m.kind === kind && m.ask_id === askId);
    if (env === undefined) continue;
    const got = ctx.batch.add(received(env));
    if (kind === "reply")
      return closeAsk(
        ctx,
        askId,
        { status: "answered", reply: env.mail_id },
        got,
      );
    const result = env.result;
    if (result === undefined || result.status === "completed")
      throw new Error("an ask's bounce carries the ended member's result");
    return closeAsk(ctx, askId, { status: "member_ended", result }, got);
  }
  if (how.cancelled) return closeAsk(ctx, askId, { status: "cancelled" });
  return how.due ? closeAsk(ctx, askId, { status: "timed_out" }) : undefined;
}

/** A reply or an ask's bounce: completes its ask while open, else is recorded only. */
export function takeAnswer(ctx: CloseContext, env: MailEnvelope): void {
  const askId = env.ask_id ?? "";
  if (askOpen(ctx.db, ctx.branchId, askId, ctx.batch))
    completeAsk(ctx, askId, { cancelled: false, due: false });
  else ctx.batch.add(received(env));
}

/** Every settle or end notice of the wait already committed counts: consume it first. */
export function committedNotices(ctx: CloseContext, waitId: string): void {
  const settles = settleMonitors(ctx.chain, ctx.batch, ctx.branchId);
  for (const env of mine(ctx))
    if (
      NOTICES.has(env.kind) &&
      env.monitor_id !== undefined &&
      settles.get(env.monitor_id) === waitId
    )
      ctx.batch.add(received(env));
}

/**
 * A settle or end notice that is a wait's: the receipt, and the wait finishes if that meets its
 * mode (a notice of a wait that already finished is recorded only). False: not a wait's.
 */
export function takeWaitNotice(ctx: CloseContext, env: MailEnvelope): boolean {
  const waitId = settleMonitors(ctx.chain, ctx.batch, ctx.branchId).get(
    env.monitor_id ?? "",
  );
  if (waitId === undefined) return false;
  const got = ctx.batch.add(received(env));
  if (openWaits(ctx.chain, ctx.batch).has(waitId))
    finishWait(ctx, waitId, { cause: got, deadline: false });
  return true;
}

function satisfied(
  mode: "all" | "any" | number,
  settled: number,
  total: number,
): boolean {
  const need = mode === "all" ? total : mode === "any" ? 1 : mode;
  return settled >= need;
}

/** The settled results this log holds (observations and received notices), by MonitorId. */
function evidenceOf(items: readonly Item[]): Map<string, StoredMemberResult> {
  const evidence = new Map<string, StoredMemberResult>();
  for (const e of items)
    if (e.type === "member_observed")
      evidence.set(e.data.monitor_id, StoredMemberResult.parse(e.data.result));
    else if (e.type === "message_received") {
      const env = e.data.envelope;
      if (env.monitor_id !== undefined && env.result !== undefined)
        evidence.set(env.monitor_id, StoredMemberResult.parse(env.result));
    }
  return evidence;
}

type Tally = {
  readonly finished: StoredMemberResult[];
  readonly parked: { member: Ref; reason: Reason }[];
  readonly pending: Ref[];
};

/** Each member of the wait, in its order: settled, parked now, or still pending. */
function tally(
  ctx: CloseContext,
  started: Extract<Item, { type: "wait_started" }>,
): Tally {
  const evidence = evidenceOf(itemsOf(ctx.chain, ctx.batch));
  const out: Tally = { finished: [], parked: [], pending: [] };
  for (const m of started.data.members) {
    const got = evidence.get(`${ctx.branchId}:${started.event_id}:${m.name}`);
    const row = memberNamed(ctx.db, m.team, m.name);
    if (got !== undefined) out.finished.push(got);
    else if (row?.state === "parked" && row.branch_id !== null)
      out.parked.push({ member: m, reason: parkReason(ctx, row.branch_id) });
    else out.pending.push(m);
  }
  return out;
}

/**
 * wait_finished from this log's evidence (member_observed and received notices), in the wait's
 * member order, once the mode is met or at the deadline; a member waiter also resumes and records
 * the call's one result. Returns the waiter's view: waiting, or waited.
 */
export function finishWait(
  ctx: CloseContext,
  waitId: string,
  how: { readonly cause?: string; readonly deadline: boolean },
): Wire<Waited> | Waiting {
  const started = itemsOf(ctx.chain, ctx.batch).find(
    (e) => e.type === "wait_started" && e.data.wait_id === waitId,
  );
  if (started?.type !== "wait_started")
    throw new Error(`no wait_started ${waitId}`);
  const { finished, parked, pending } = tally(ctx, started);
  const { mode, members } = started.data;
  const met = satisfied(mode, finished.length, members.length);
  if (!met && !how.deadline) return { status: "waiting", wait_id: waitId };
  const done = { finished, parked, pending, timed_out: !met };
  const finishedId = ctx.batch.add({
    ...HOST,
    type: "wait_finished",
    data: { wait_id: waitId, ...done },
  });
  const waited: Wire<Waited> = {
    status: "waited",
    ...done,
    finished: finished.map((r) => publicResult(r, ctx.read)),
  };
  if (ctx.chain.fold.team.teamLog) return waited;
  resume(ctx, { kind: "wait", id: waitId }, how.cause ?? finishedId);
  ctx.batch.add(toolResult(callOf(waitId), waited));
  return waited;
}

const ParkedLine = z.object({ data: z.object({ reason: ParkReason }) });

/** Why a parked member is parked: its log's last park. */
function parkReason(ctx: CloseContext, branch: string): Reason {
  const [row] = z
    .array(z.object({ line: z.instanceof(Uint8Array) }))
    .parse(
      ctx.db.all(
        "SELECT line FROM events WHERE branch_id = ? AND type = 'parked' ORDER BY seq DESC LIMIT 1",
        [branch],
      ),
    );
  if (row === undefined)
    throw new Error(`a parked member ${branch} has a park`);
  return ParkedLine.parse(JSON.parse(new TextDecoder().decode(row.line))).data
    .reason;
}
