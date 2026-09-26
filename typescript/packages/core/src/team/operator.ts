import { z } from "zod";
import { sha256Hex } from "../hash";
import {
  canonicalize,
  EventId,
  type Json,
  type KnownEvent,
  type MailEnvelope,
  type MemberRef,
  type Principal,
  principalKey,
  TeamRefusal,
} from "../log";
import type { Tx } from "../store/driver";
import type { Chain } from "../verify";
import type { AskOpened } from "./ask";
import type { Batch } from "./batch";
import { type Refusal, type Refused, refusal, refusedOf } from "./call";
import type { CancelRequested } from "./cancel";
import type { ReadText, Waiting } from "./close";
import type { PutText } from "./mail";
import type { Sent, Started } from "./ops";
import type { Request, Target } from "./request";
import type { Waited, Wire } from "./results";
import { memberNamed, memberRows, type TeamRow } from "./rows";

// An operator request in the team log (design §4.4; spec/schema/README.md, "Teams"): its key is
// looked up first, then operator_request opens it, and it is decided by the same ops as a model
// call. Reference: spec/tools/fixtures/ops_request.py (open_request, keyed, recorded).

export type OperatorOp = "start" | "send" | "ask" | "wait" | "cancel";

export type OperatorInput = {
  readonly requestId: string;
  readonly op: OperatorOp;
  readonly principal: Principal;
  /**
   * The method's parameters as spec/api.json names them (snake_case), without idempotency_key and
   * with every omitted option left out: body_hash is its RFC 8785 sha256.
   */
  readonly body: Readonly<Record<string, Json>>;
  readonly idempotencyKey?: string | undefined;
};

/** What an operator request reads, inside the team log's append. */
export type OperatorContext = {
  readonly tx: Tx;
  /** The team log's committed chain. */
  readonly chain: Chain;
  readonly batch: Batch;
  readonly put: PutText;
  readonly read: ReadText;
  readonly team: TeamRow;
};

/**
 * A request's outcome, as the team log holds it: what its op returned. A replayed ask or wait
 * re-attaches: its id, open or waiting, whatever the team log has recorded for it since.
 */
export type Recorded =
  | Started
  | Sent
  | Refused
  | CancelRequested
  | AskOpened
  | Wire<Waited>
  | Waiting;

/** Open: decide it with the op. Recorded: its key replays an earlier request's outcome. */
export type Opened =
  | { readonly kind: "open"; readonly request: Request }
  | { readonly kind: "recorded"; readonly outcome: Recorded };

const Receipt = z.strictObject({
  principal_key: z.string(),
  body_hash: z.string(),
  request_id: z.string(),
});

const utf8 = new TextEncoder();

function bodyHash(body: OperatorInput["body"]): string {
  const bytes = canonicalize(body);
  if (!bytes.ok) throw new Error(bytes.error.message);
  return sha256Hex(utf8.encode(bytes.value));
}

/**
 * The key's binding, looked up before anything is recorded: the same principal and body replay
 * the bound request's outcome and append nothing; another principal, then another body, is
 * refused, recorded as a request without the key (the key stays bound to the first).
 */
export async function openOperator(
  ctx: OperatorContext,
  input: OperatorInput,
): Promise<Opened> {
  const key = input.idempotencyKey;
  if (key === undefined) return { kind: "open", request: opened(ctx, input) };
  const receipt = z.array(Receipt).parse(
    await ctx.tx.all(
      `SELECT principal_key, body_hash, request_id FROM operator_receipts
          WHERE tenant_id = ? AND team_id = ? AND op = ? AND idempotency_key = ?`,
      [ctx.team.tenant_id, ctx.team.team_id, input.op, key],
    ),
  )[0];
  if (receipt === undefined)
    return { kind: "open", request: opened(ctx, input) };
  const code =
    receipt.principal_key !== principalKey(input.principal)
      ? "idempotency_key_principal_mismatch"
      : receipt.body_hash !== bodyHash(input.body)
        ? "idempotency_key_reused"
        : undefined;
  if (code === undefined)
    return {
      kind: "recorded",
      outcome: recorded(ctx.chain, ctx.team, receipt.request_id),
    };
  const { idempotencyKey: _bound, ...unkeyed } = input;
  const outcome = opened(ctx, unkeyed).refuse(refusal(code));
  return { kind: "recorded", outcome };
}

/** operator_request, whose own event is the provenance's root request, then the request. */
function opened(ctx: OperatorContext, input: OperatorInput): Request {
  const { tx, batch, team } = ctx;
  const header = ctx.chain.segments[0]?.header;
  if (header === undefined) throw new Error("a team log has a header");
  const root = {
    thread_id: header.thread_id,
    event_id: EventId.parse(batch.nextId()),
  };
  const provenance = {
    principal: input.principal,
    root_request: root,
    via: [],
  };
  const rid = input.requestId;
  batch.add({
    type: "operator_request",
    type_version: 1,
    critical: true,
    actor: { kind: "host", principal: input.principal },
    data: {
      request_id: rid,
      op: input.op,
      principal: input.principal,
      ...(input.idempotencyKey === undefined
        ? {}
        : { idempotency_key: input.idempotencyKey }),
      body_hash: bodyHash(input.body),
      provenance,
    },
  });
  // An operator acting for a principal of another tenant is denied (Phase 1 policy).
  const allow = input.principal.tenant === team.tenant_id;
  return {
    tx,
    batch,
    put: ctx.put,
    team,
    from: { operator: rid },
    provenance,
    causal: root,
    mailId: `${header.branch_id}:${rid}`,
    self: undefined,
    decide: (op, target) => {
      batch.add({
        type: "message_policy_decided",
        type_version: 1,
        critical: true,
        actor: { kind: "host" },
        data: {
          op,
          decision: allow ? "allow" : "deny",
          source: allow ? "team" : "default",
          target,
          request_id: rid,
        },
      });
      return allow ? undefined : refusal("forbidden");
    },
    parent: () => leadParent(tx, team),
    refuse: (value: Refusal) => {
      batch.add({
        type: "operator_refused",
        type_version: 1,
        critical: true,
        actor: { kind: "host" },
        data: {
          request_id: rid,
          // Only a reply refuses with its own codes; an operator request never replies.
          code: TeamRefusal.parse(value.refused),
          ...(value.detail === undefined ? {} : { detail: value.detail }),
        },
      });
      return refusedOf(value);
    },
    done: (value) => value,
  };
}

/** An operator start's parent is the lead's thread_started, which carries the team. */
async function leadParent(
  tx: Tx,
  team: TeamRow,
): ReturnType<Request["parent"]> {
  const lead = (await memberRows(tx, team.team_id)).find(
    (r) => r.role === "lead",
  );
  const first = z
    .array(z.strictObject({ event_id: z.string() }))
    .parse(
      await tx.all(
        "SELECT event_id FROM events WHERE branch_id = ? AND seq = 1",
        [lead?.branch_id ?? null],
      ),
    )[0];
  if (lead === undefined || lead.branch_id === null || first === undefined)
    throw new Error(`team ${team.team_id} has no lead log`);
  return {
    thread_id: lead.thread_id,
    branch_id: lead.branch_id,
    event_id: first.event_id,
    relation: "team_member",
  };
}

/** An operator's target: the member a ref names, known in this team at its generation. */
export function refTarget(tx: Tx, team: TeamRow, ref: MemberRef): Target {
  return {
    name: ref.name,
    row: async () => {
      const row =
        ref.team === team.team_id && ref.tenant === team.tenant_id
          ? await memberNamed(tx, team.team_id, ref.name)
          : undefined;
      if (row === undefined || ref.generation > row.generation)
        return refusal("unknown_member");
      return ref.generation < row.generation ? refusal("stale_member") : row;
    },
  };
}

/**
 * The outcome the team log recorded for request `rid`: its refusal, the member it started, the
 * mail it sent or the cancel it requested; an ask or a wait re-attaches, with its id.
 */
function recorded(chain: Chain, team: TeamRow, rid: string): Recorded {
  const events = chain.events.flatMap((l) =>
    l.kind === "event" ? [l.event] : [],
  );
  const request = events.find(
    (e) => e.type === "operator_request" && e.data.request_id === rid,
  );
  const branch = chain.segments[0]?.header.branch_id;
  for (const e of events) {
    const outcome =
      e.type === "wait_started" && e.data.wait_id === `${branch}:${rid}`
        ? { status: "waiting" as const, wait_id: e.data.wait_id }
        : outcomeOf(e, team, rid, request?.event_id);
    if (outcome !== undefined) return outcome;
  }
  throw new Error(`request ${rid} recorded no outcome this build replays`);
}

function outcomeOf(
  e: KnownEvent,
  team: TeamRow,
  rid: string,
  requestEvent: string | undefined,
): Recorded | undefined {
  switch (e.type) {
    case "operator_refused":
      return e.data.request_id !== rid
        ? undefined
        : {
            status: "refused",
            code: e.data.code,
            ...(e.data.detail === undefined ? {} : { detail: e.data.detail }),
          };
    case "member_started":
      return e.data.provenance?.root_request.event_id === requestEvent
        ? { member: e.data.member, status: "started" }
        : undefined;
    case "message_sent":
      return "operator" in e.data.envelope.from &&
        e.data.envelope.from.operator === rid
        ? mailOutcome(e.data.envelope, team)
        : undefined;
    default:
      return undefined;
  }
}

/** An operator's own mail: its send's message, its ask, or its cancel's request. */
function mailOutcome(env: MailEnvelope, team: TeamRow): Recorded | undefined {
  if (env.kind === "message") return { id: env.mail_id, status: "sent" };
  if (
    env.kind === "ask" &&
    env.ask_id !== undefined &&
    env.deadline !== undefined
  )
    return { status: "open", ask_id: env.ask_id, deadline: env.deadline };
  if (env.kind !== "cancel" || env.to === "team_log" || "caller" in env.to)
    return undefined;
  const member = { tenant: team.tenant_id, team: env.team, ...env.to };
  return { member, status: "cancel_requested" };
}
