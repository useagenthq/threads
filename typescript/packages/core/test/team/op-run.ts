import { z } from "zod";
import type { EventOf } from "../../src/fold/state";
import { EndedResult, MemberRef, Principal, ThreadId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { ok } from "../../src/result";
import type { EventDraft, Writer } from "../../src/store";
import { ask, openAsk, reply } from "../../src/team/ask";
import { Batch } from "../../src/team/batch";
import { type CallContext, callRequest, named } from "../../src/team/call";
import { cancel, requestCancel } from "../../src/team/cancel";
import { TEAM_CONSTANTS } from "../../src/team/constants";
import { type ConsumeContext, consume } from "../../src/team/consume";
import { deadline } from "../../src/team/deadline";
import { resolveDefinition } from "../../src/team/dynamic";
import { openOperator, refTarget } from "../../src/team/operator";
import { type StartPlan, send, start } from "../../src/team/ops";
import { turnProvenance } from "../../src/team/provenance";
import type { Request } from "../../src/team/request";
import { type TeamRow, teamOfLog } from "../../src/team/rows";
import { type Settlement, settle } from "../../src/team/settle";
import { monitor, openWait, wait, waitMembers } from "../../src/team/watch";
import { StartInput } from "../../src/tools/team-inputs";
import { DOC, TEAM, type Vector, vectorMint } from "./vectors";

// One op vector's op under its writer, at the writer's clock, through this runtime's own store
// ops: shared by the vector suite and the two-process races.

/** What running an op reads of its vector. */
export type Op = Pick<Vector, "op" | "input" | "given">;

const Args = {
  send: z.object({ to: z.string(), text: z.string() }),
  ask: z.object({ to: z.string(), question: z.string() }),
  reply: z.object({ ask_id: z.string(), text: z.string() }),
  wait: z.object({ members: z.array(z.string()) }),
  monitor: z.object({ member: z.string() }),
};
const put = (): never => {
  throw new Error("the vectors carry no body above the inline cap");
};
const read = put;

/** Appends what `decide` adds to a batch under the op's writer, at the vector's clock. */
async function decided(
  w: Writer,
  decide: (ctx: ConsumeContext) => Promise<void>,
): Promise<void> {
  const header = w.chain.segments.at(-1)?.header;
  if (header === undefined) throw new Error("a writer has a header");
  const appended = await w.appendDecided(async (tx) => {
    const batch = new Batch(tx.chain.fold.seq, tx.now, vectorMint);
    await decide({
      tx: tx.tx,
      chain: tx.chain,
      batch,
      threadId: header.thread_id,
      branchId: header.branch_id,
      read,
    });
    return ok(batch.drafts);
  });
  if (!("ok" in appended) || !appended.ok) throw new Error("append failed");
}

/** A model call's outcome: what the op returns, else its one tool_result as the value it records. */
async function callOp(w: Writer, v: Op): Promise<unknown> {
  const callId = z.string().parse(v.input["call_id"]);
  const call = knownEvents(w.chain).find(
    (e): e is EventOf<"tool_call"> =>
      e.type === "tool_call" && e.data.call_id === callId,
  );
  if (call === undefined) throw new Error(`no call ${callId}`);
  let out: unknown;
  await decided(w, async (ctx) => {
    out = await modelOp({ ...ctx, call, put, read }, v);
  });
  if (out !== undefined) return out;
  const result = knownEvents(w.chain).findLast((e) => e.type === "tool_result");
  if (result?.type !== "tool_result") throw new Error("no result");
  return JSON.parse(result.data.preview);
}

async function modelOp(c: CallContext, v: Op): Promise<unknown> {
  const args = v.input["args"];
  switch (v.op) {
    case "send": {
      const parsed = Args.send.parse(args);
      return send(
        await callRequest(c),
        named(c, parsed.to),
        parsed.text,
        limits(v),
      );
    }
    case "start": {
      const parsed = StartInput.parse(args);
      return start(await callRequest(c), parsed, startPlan(v, parsed));
    }
    case "ask":
      return ask(c, Args.ask.parse(args), {
        limits: limits(v),
        headroom: async () => v.given.headroom ?? true,
      });
    case "reply":
      return reply(c, Args.reply.parse(args));
    case "monitor":
      return monitor(c, Args.monitor.parse(args));
    case "cancel":
      return cancel(c, Args.monitor.parse(args));
    default:
      return wait(c, Args.wait.parse(args));
  }
}

function startPlan(v: Op, args: z.infer<typeof StartInput>): StartPlan {
  return {
    agents: agents(v),
    resolved: resolveDefinition(v.given.templates?.[args.agent], args),
    limits: limits(v),
    headroom: async () => v.given.headroom ?? true,
    threadId: ThreadId.parse(v.input["thread_id"]),
  };
}

const Body = {
  send: z.object({ to: MemberRef, text: z.string() }),
  ask: z.object({
    to: MemberRef,
    question: z.string(),
    timeout_ms: z.number().optional(),
  }),
  wait: z.object({
    members: z.array(MemberRef),
    mode: z.union([z.enum(["all", "any"]), z.number()]).optional(),
    timeout_ms: z.number().optional(),
  }),
  cancel: z.object({ member: MemberRef }),
  any: z.record(z.string(), z.json()),
};

/**
 * An operator request's outcome: what the op returns, or what its key replays. A wait's mode
 * above its member count is refused before the writer, as the handle does.
 */
async function operatorOp(w: Writer, v: Op): Promise<unknown> {
  if (v.op === "wait") {
    const body = Body.wait.parse(v.input["body"]);
    if (waitMembers(body.members, body.mode) === "invalid_request")
      return { code: "invalid_request", status: "refused" };
  }
  let out: unknown;
  await decided(w, async (ctx) => {
    const team = await teamOfLog(ctx.tx, ctx.branchId);
    if (team === undefined) throw new Error("not a team log");
    const opened = await openOperator(
      { ...ctx, put, team },
      {
        requestId: z.string().parse(v.input["request_id"]),
        op: z.enum(["start", "send", "ask", "wait", "cancel"]).parse(v.op),
        principal: Principal.parse(v.input["principal"]),
        body: Body.any.parse(v.input["body"]),
        idempotencyKey: z.string().optional().parse(v.input["idempotency_key"]),
      },
    );
    if (opened.kind === "recorded") {
      out = opened.outcome;
      return;
    }
    out = await operatorDecide(ctx, opened.request, team, v);
  });
  return out;
}

function operatorDecide(
  ctx: ConsumeContext,
  req: Request,
  team: TeamRow,
  v: Op,
): Promise<unknown> {
  const body = v.input["body"];
  switch (v.op) {
    case "send": {
      const b = Body.send.parse(body);
      return send(req, refTarget(ctx.tx, team, b.to), b.text, limits(v));
    }
    case "ask": {
      const b = Body.ask.parse(body);
      return openAsk(req, refTarget(ctx.tx, team, b.to), b.question, {
        limits: limits(v),
        headroom: async () => v.given.headroom ?? true,
        ...(b.timeout_ms === undefined ? {} : { timeoutMs: b.timeout_ms }),
      });
    }
    case "wait": {
      const b = Body.wait.parse(body);
      const members = waitMembers(b.members, b.mode);
      if (members === "invalid_request") throw new Error("checked before");
      const targets = members.map((m) => refTarget(ctx.tx, team, m));
      return openWait(req, ctx, targets, {
        mode: b.mode ?? "all",
        timeoutMs: b.timeout_ms ?? TEAM_CONSTANTS.askWaitDefaultMs,
      });
    }
    case "cancel":
      return requestCancel(
        req,
        refTarget(ctx.tx, team, Body.cancel.parse(body).member),
      );
    default: {
      const b = StartInput.parse(body);
      return start(req, b, startPlan(v, b));
    }
  }
}

const limits = (v: Op) => ({
  concurrent: v.given.concurrent ?? 4,
  mailbox: v.given.mailbox ?? 100,
});

/**
 * The agents the worlds' team lists, each with the config_hash its member logs pin, and the
 * vector's dynamic agents with the hash its input gives the define's pin.
 */
function agents(v: Op): ReadonlyMap<string, { readonly configHash: string }> {
  const out = new Map<string, { readonly configHash: string }>();
  const hash = z.string().optional().parse(v.input["config_hash"]);
  for (const name of Object.keys(v.given.templates ?? {}))
    if (hash !== undefined) out.set(name, { configHash: hash });
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

async function consumeOp(w: Writer): Promise<unknown> {
  let out: unknown;
  await decided(w, async (ctx) => {
    const got = await consume(ctx);
    out =
      got.status === "nothing_pending"
        ? got
        : { status: got.status, mail_ids: got.mailIds };
  });
  return out;
}

async function deadlineOp(w: Writer, v: Op): Promise<unknown> {
  let out: unknown;
  await decided(w, async (ctx) => {
    out = await deadline(ctx, z.string().parse(v.input["id"]));
  });
  return out;
}

function turnEnd(v: Op): EventDraft {
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
async function settleOp(w: Writer, v: Op): Promise<unknown> {
  const events = knownEvents(w.chain);
  const idle = v.op === "idle";
  const how: Settlement = idle
    ? { status: "completed", output: lastText(events) }
    : endedOf(v.input["result"]);
  await decided(w, async (ctx) => {
    ctx.batch.add(turnEnd(v));
    const provenance = await turnProvenance(ctx.tx, ctx.chain);
    if (provenance === undefined) throw new Error("no turn");
    await settle({ ...ctx, provenance, put }, how);
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

/** The op under `w`: its outcome as the vector states it. */
export function runOn(w: Writer, v: Op): Promise<unknown> {
  switch (v.op) {
    case "send":
    case "start":
    case "ask":
    case "wait":
    case "cancel":
      return "request_id" in v.input ? operatorOp(w, v) : callOp(w, v);
    case "reply":
    case "monitor":
      return callOp(w, v);
    case "consume":
      return consumeOp(w);
    case "deadline":
      return deadlineOp(w, v);
    case "idle":
    case "end":
      return settleOp(w, v);
    default:
      throw new Error(`op ${v.op} runs on a store, not a writer`);
  }
}
