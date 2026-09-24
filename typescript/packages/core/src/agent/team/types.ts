import type { MailId, MemberRef, TeamId } from "../../log";
import type { Agent, RunInput, StreamEvent } from "../agent";
import type { RunResult } from "../result";
import type { RunOptions } from "../run";

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

/** Why a start was refused. team_closed: the lead ended, which closes the team. */
export type StartRefusal =
  | "forbidden"
  | "unknown_agent"
  | "concurrency_cap"
  | "budget_exceeded"
  | "team_closed";

/** The model's start tool result. */
export type StartResult =
  | { readonly status: "started"; readonly member: MemberRef }
  | { readonly status: "refused"; readonly code: StartRefusal };

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
