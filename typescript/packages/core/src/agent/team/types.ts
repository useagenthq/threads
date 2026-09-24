import type { z } from "zod";
import type {
  AskId,
  BudgetExceededData,
  MailId,
  MemberErrorCode as MemberErrorCodeSchema,
  MemberRef,
  ParkReason as ParkReasonSchema,
  TeamId,
  ThreadId,
} from "../../log";
import type { InvalidDefinition } from "../../team/dynamic";
import type { Agent, RunInput, StreamEvent } from "../agent";
import type { RunResult } from "../result";
import type { RunOptions } from "../run";

type MemberErrorCode = z.infer<typeof MemberErrorCodeSchema>;
type ParkReason = z.infer<typeof ParkReasonSchema>;
type BudgetExceeded = z.infer<typeof BudgetExceededData>;

// The public face of a team lead (spec/api.json TeamAgent, TeamRunResult, Team): agent({team})
// returns a TeamAgent, whose run() result also carries the team's handle.

/** Which team: team.ref, for openTeam in another process. */
export type TeamRef = {
  readonly tenant: string;
  readonly id: TeamId;
};

/**
 * A team's handle. Phase 1 lane 21F adds its operator methods (start, send, ask, wait, cancel,
 * members, events, askStatus); until then it names the team.
 */
export type Team = {
  readonly ref: TeamRef;
};

export type { MemberRef } from "../../log";

/**
 * Why a start was refused. team_closed: the lead ended, which closes the team.
 * invalid_definition: a label, instructions, tools or model the start may not choose.
 */
export type StartRefusal =
  | "forbidden"
  | "unknown_agent"
  | "concurrency_cap"
  | "budget_exceeded"
  | "team_closed"
  | "invalid_definition";

export type { InvalidDefinition } from "../../team/dynamic";

/** The model's start tool result. */
export type StartResult =
  | { readonly status: "started"; readonly member: MemberRef }
  | {
      readonly status: "refused";
      readonly code: StartRefusal;
      /** Present exactly when code is invalid_definition. */
      readonly detail?: InvalidDefinition;
    };

/**
 * A dynamic agent (dynamicAgent()): a template in a lead's team whose members a start defines
 * within what its code pins. It runs only as a team member.
 */
export type DynamicAgent<_Deps = undefined, _Output = string> = {
  readonly name: string;
};

/** Why a send was refused. stale_member: the member was restarted under a newer generation. */
export type SendRefusal =
  | "forbidden"
  | "unknown_member"
  | "stale_member"
  | "member_ended"
  | "self"
  | "mailbox_full"
  | "team_closed";

/** The model's send tool result. */
export type SendResult =
  | { readonly status: "sent"; readonly id: MailId }
  | { readonly status: "refused"; readonly code: SendRefusal };

/** Why an ask was refused: a send's refusals, or no headroom for one request of its model. */
export type AskRefusal = SendRefusal | "budget_exceeded";

/** Why a reply was refused. */
export type ReplyRefusal = "unknown_ask" | "already_replied" | "ask_closed";

/** Why a wait or monitor was refused. */
export type ObserveRefusal = "forbidden" | "unknown_member" | "stale_member";

/** What a settled member returned; a large output is read back from the artifact store. */
export type MemberResult = { readonly member: MemberRef } & (
  | { readonly status: "completed"; readonly output: string }
  | {
      readonly status: "failed";
      readonly error: {
        readonly code: MemberErrorCode;
        readonly message: string;
      };
    }
  | { readonly status: "cancelled" }
  | { readonly status: "budget_exhausted"; readonly budget: BudgetExceeded }
  | { readonly status: "handed_off"; readonly toThread: ThreadId }
);

/** How an ask ended. needs_input and uncertain are remote members' (Phase 4). */
export type AskOutcome =
  | {
      readonly status: "answered";
      readonly askId: AskId;
      readonly text: string;
      readonly member: MemberRef;
    }
  | { readonly status: "timed_out"; readonly askId: AskId }
  | {
      readonly status: "member_ended";
      readonly askId: AskId;
      readonly result: MemberResult;
    }
  | { readonly status: "cancelled"; readonly askId: AskId }
  | {
      readonly status: "needs_input";
      readonly askId: AskId;
      readonly prompt: string;
    }
  | { readonly status: "uncertain"; readonly askId: AskId };

/** The model's ask tool result. */
export type AskResult =
  | AskOutcome
  | { readonly status: "refused"; readonly code: AskRefusal };

/** The model's reply tool result. */
export type ReplyResult =
  | { readonly status: "sent"; readonly id: MailId }
  | { readonly status: "refused"; readonly code: ReplyRefusal };

/** A finished wait: what settled, what is parked, what is still pending. */
export type Waited = {
  readonly status: "waited";
  readonly finished: readonly MemberResult[];
  readonly parked: readonly {
    readonly member: MemberRef;
    readonly reason: ParkReason;
  }[];
  readonly pending: readonly MemberRef[];
  /** The deadline passed with the mode unmet. */
  readonly timedOut: boolean;
};

/** The model's wait tool result. */
export type WaitResult =
  | Waited
  | { readonly status: "refused"; readonly code: ObserveRefusal };

/** The model's monitor tool result. */
export type MonitorResult =
  | { readonly status: "monitoring"; readonly member: MemberRef }
  | { readonly status: "ended"; readonly result: MemberResult }
  | { readonly status: "refused"; readonly code: ObserveRefusal };

/** agent({teamLimits}): the team's limits. */
export type TeamLimits = {
  /** Most members running at once; a start past it is refused concurrency_cap. Default 4. */
  readonly concurrent?: number;
  /** Most pending mails per member; a send past it is refused mailbox_full. Default 100. */
  readonly mailbox?: number;
};

/** A RunResult that also carries the lead's team, whatever the status. */
export type TeamRunResult<Output = string> = RunResult<Output> & {
  readonly team: Team;
};

/** What TeamAgent.stream() returns: a RunStream whose result is a TeamRunResult. */
export type TeamRunStream<Output = string> = AsyncIterable<StreamEvent> & {
  readonly result: Promise<TeamRunResult<Output>>;
};

/** An agent defined with a team: its runs return the team as well. */
export type TeamAgent<Deps = undefined, Output = string> = Omit<
  Agent<Deps, Output>,
  "run" | "stream"
> & {
  readonly run: (
    input: RunInput,
    options?: RunOptions<Deps>,
  ) => Promise<TeamRunResult<Output>>;
  readonly stream: (
    input: RunInput,
    options?: RunOptions<Deps>,
  ) => TeamRunStream<Output>;
};
