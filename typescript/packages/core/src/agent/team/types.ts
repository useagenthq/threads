import type { MailId, MemberRef } from "../../log";
import type { InvalidDefinition } from "../../team/dynamic";
import type { SendRefusal } from "../../team/results";
import type { Agent, RunInput, StreamEvent } from "../agent";
import type { RunResult } from "../result";
import type { RunOptions } from "../run";
import type { Team } from "./handle-types";

// The public face of a team lead (spec/api.json TeamAgent, TeamRunResult): agent({team})
// returns a TeamAgent, whose run() result also carries the team's handle.

export type { MemberRef } from "../../log";
export type { Team, TeamRef } from "./handle-types";

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
export type {
  AskOutcome,
  AskRefusal,
  AskResult,
  CancelRefusal,
  CancelResult,
  MemberResult,
  MonitorResult,
  ObserveRefusal,
  ReplyRefusal,
  ReplyResult,
  SendRefusal,
  Waited,
  WaitResult,
} from "../../team/results";

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

/** The model's send tool result. */
export type SendResult =
  | { readonly status: "sent"; readonly id: MailId }
  | { readonly status: "refused"; readonly code: SendRefusal };

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
