import type {
  KnownEvent,
  MailId,
  MemberName,
  MemberRef,
  Principal,
  RequestId,
  TeamId,
} from "../../log";
import type { InvalidDefinition } from "../../team/dynamic";
import type { MemberResult, SendRefusal } from "../../team/results";
import type { StartRefusal } from "./types";

// The operator's handle on a team (spec/api.json Team, Team* results, TeamMember, TeamItem): what
// run().team and openTeam return. Lane 21E's ask, wait, cancel and askStatus join it.

/** Which team: team.ref, for openTeam in another process. */
export type TeamRef = {
  readonly tenant: string;
  readonly id: TeamId;
};

/**
 * Refusals only Team methods return. busy: the team log stayed locked past the busy bound, so
 * nothing was recorded; retrying with the same key is safe.
 */
export type OperatorRefusal =
  | "busy"
  | "idempotency_key_reused"
  | "idempotency_key_principal_mismatch";

/** team.start's result. */
export type TeamStartResult =
  | { readonly status: "started"; readonly member: MemberRef }
  | {
      readonly status: "refused";
      readonly code: StartRefusal | OperatorRefusal;
      /** Present exactly when code is invalid_definition. */
      readonly detail?: InvalidDefinition;
    };

/** team.send's result. */
export type TeamSendResult =
  | { readonly status: "sent"; readonly id: MailId }
  | {
      readonly status: "refused";
      readonly code: SendRefusal | OperatorRefusal;
    };

/** starting: its log not opened yet. idle: its latest task is done; mail wakes it. */
export type MemberState = "starting" | "running" | "idle" | "parked" | "ended";

/** One row of team.members(). */
export type TeamMember = {
  readonly name: MemberName;
  readonly ref: MemberRef;
  readonly agent: string;
  readonly state: MemberState;
  /** Present once idle or ended. */
  readonly result?: MemberResult;
  /** The display label its start gave, if any. Never shown to a model. */
  readonly label?: string;
};

/** A position in the team feed, to resume team.events after. */
export type TeamCursor = { readonly epoch: number; readonly offset: number };

/** Which log a feed item came from. */
export type TeamSource =
  | { readonly kind: "member"; readonly member: MemberRef }
  | {
      readonly kind: "operator";
      readonly principal: Principal;
      readonly request: RequestId;
    }
  | { readonly kind: "team" };

/** One item of team.events(). */
export type TeamItem =
  | {
      readonly kind: "event";
      readonly cursor: TeamCursor;
      readonly source: TeamSource;
      /** The event exactly as stored: a result keeps its artifact ref. */
      readonly event: KnownEvent;
    }
  | { readonly kind: "epoch_restarted"; readonly cursor: TeamCursor };

/** Every Team method's retry key: the same key, principal and body replay the first outcome. */
type Keyed = { readonly idempotencyKey?: string };

/** team.start's options: a label on any start; the rest for a dynamic agent only. */
export type TeamStartOptions = Keyed & {
  readonly label?: string;
  readonly instructions?: string;
  readonly tools?: readonly string[];
  readonly model?: string;
};

/**
 * Your code's handle on a team: RunResult.team, or openTeam. Every request is recorded in the
 * team log as the principal it acts for; only busy, returned before the log could be written, is
 * not.
 */
export type Team = {
  readonly ref: TeamRef;
  /** Starts a member from an agent the team lists, with task as its first input. */
  readonly start: (
    agent: string,
    task: string,
    options?: TeamStartOptions,
  ) => Promise<TeamStartResult>;
  /** Sends a member a message: its next input, which wakes it if idle. */
  readonly send: (
    to: MemberRef,
    text: string,
    options?: Keyed,
  ) => Promise<TeamSendResult>;
  /** Every member, the lead included, with its state and, once settled, its result. */
  readonly members: () => Promise<readonly TeamMember[]>;
  /** The team's audit feed: every committed event of every team log, as stored. */
  readonly events: (options?: {
    readonly after?: TeamCursor;
  }) => AsyncIterable<TeamItem>;
};
