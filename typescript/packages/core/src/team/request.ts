import type { Provenance } from "../log";
import type { Tx } from "../store/driver";
import type { Batch } from "./batch";
import type { Refusal, Refused } from "./call";
import type { Envelope, PutText } from "./mail";
import type { MemberRow, TeamRow } from "./rows";

// One team request under its writer (spec/schema/README.md, "Teams"): a model call (its pending
// tool_call, call.ts) or an operator request (its operator_request in the team log, operator.ts).
// The ops decide the same way for both; what differs (who sends, the grant, how a member is
// named, how the outcome is recorded) is here. Reference: spec/tools/fixtures/ops_request.py.

/** The ops the Phase 1 policy decides. */
export type PolicyOp = "start" | "send" | "ask";

/** The member an op addresses: its name (the decision's target) and its row, read when due. */
export type Target = {
  readonly name: string;
  readonly row: () => Promise<MemberRow | Refusal>;
};

/** thread_started.parent of a member this request starts. */
export type Parent = {
  readonly thread_id: string;
  readonly branch_id: string;
  readonly event_id: string;
  readonly relation: "team_member";
};

export type Request = {
  /** The request's append transaction: every read and write goes through it. */
  readonly tx: Tx;
  readonly batch: Batch;
  readonly put: PutText;
  readonly team: TeamRow;
  /** The sender: the caller's MemberRef, or `{operator: request_id}`. */
  readonly from: Envelope["from"];
  readonly provenance: Provenance;
  /** The request that causes its mail: the tool_call, or the operator_request. */
  readonly causal: { readonly thread_id: string; readonly event_id: string };
  /** `<sender branch_id>:<call_id or request_id>`. */
  readonly mailId: string;
  /** The sender's own row; undefined for the operator. */
  readonly self: MemberRow | undefined;
  /** Records the Phase 1 decision on `target`: the team's grant, else default deny (forbidden). */
  readonly decide: (op: PolicyOp, target: string) => Refusal | undefined;
  /** member_started.parent, given member_started's own event id. */
  readonly parent: (startedId: string) => Promise<Parent>;
  /** Records a refusal (the call's tool_result, or operator_refused) and returns it. */
  readonly refuse: (refusal: Refusal) => Refused;
  /** Records a success (the call's tool_result; an operator's needs nothing) and returns it. */
  readonly done: <T extends object>(value: T) => T;
};
