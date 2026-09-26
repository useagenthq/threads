import type {
  AskId,
  KnownEvent,
  MailId,
  MemberName,
  MemberRef,
  Principal,
  RequestId,
  TeamId,
} from "../../log";
import type { InvalidDefinition } from "../../team/dynamic";
import type {
  AskOutcome,
  AskRefusal,
  CancelRefusal,
  MemberResult,
  ObserveRefusal,
  SendRefusal,
  Waited,
} from "../../team/results";
import type { StartRefusal } from "./types";

// The operator's handle on a team (spec/api.json Team, Team* results, TeamMember, TeamItem): what
// run().team and openTeam return.

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

/**
 * team.ask's result: how the ask ended, or why it was refused. invalid_request (a timeoutMs that
 * is not a positive integer) is returned before any writer, so, like busy, it is never logged.
 */
export type TeamAskResult =
  | AskOutcome
  | {
      readonly status: "refused";
      readonly code: AskRefusal | OperatorRefusal | "invalid_request";
    };

/**
 * team.wait's result. invalid_request (no members, a mode that is not a positive integer no
 * greater than the member count, or a timeoutMs that is not a positive integer) is returned
 * before any writer, so, like busy, it is never logged.
 */
export type TeamWaitResult =
  | Waited
  | {
      readonly status: "refused";
      readonly code: ObserveRefusal | OperatorRefusal | "invalid_request";
    };

/** team.cancel's result: cancel_requested is durable; the member ends at its next step. */
export type TeamCancelResult =
  | { readonly status: "cancel_requested"; readonly member: MemberRef }
  | {
      readonly status: "refused";
      readonly code: CancelRefusal | OperatorRefusal;
    };

/** team.askStatus's answer: open, not_found, or how the ask ended. */
export type AskStatus =
  | {
      readonly status: "open";
      readonly askId: AskId;
      readonly deadline: number;
    }
  | { readonly status: "not_found"; readonly askId: AskId }
  | AskOutcome;

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

/**
 * team.ask's options. timeoutMs: a positive integer, omitted for the default (120 s), and never
 * more than it; anything else is refused invalid_request.
 */
export type TeamAskOptions = Keyed & { readonly timeoutMs?: number };

/**
 * team.wait's options. mode: all (the default), any, or how many must settle; any never cancels
 * the others. timeoutMs: a positive integer, omitted for the default (120 s), and never more
 * than it. A mode or timeoutMs that is not one is refused invalid_request.
 */
export type TeamWaitOptions = Keyed & {
  readonly mode?: "all" | "any" | number;
  readonly timeoutMs?: number;
};

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
  /** Asks a member a question and waits for its reply, its end, or the deadline. */
  readonly ask: (
    to: MemberRef,
    question: string,
    options?: TeamAskOptions,
  ) => Promise<TeamAskResult>;
  /**
   * Waits until members settle (become idle or end), or the deadline. Settlement is durable
   * evidence: a member that went idle and started again still counts.
   */
  readonly wait: (
    members: readonly MemberRef[],
    options?: TeamWaitOptions,
  ) => Promise<TeamWaitResult>;
  /** Requests a member's cancel: durable at once, applied at the member's next step. */
  readonly cancel: (
    member: MemberRef,
    options?: Keyed,
  ) => Promise<TeamCancelResult>;
  /** An ask's state from the team log. A pure read; it never closes an ask. */
  readonly askStatus: (askId: AskId) => Promise<AskStatus>;
  /** Every member, the lead included, with its state and, once settled, its result. */
  readonly members: () => Promise<readonly TeamMember[]>;
  /** The team's audit feed: every committed event of every team log, as stored. */
  readonly events: (options?: {
    readonly after?: TeamCursor;
  }) => AsyncIterable<TeamItem>;
};
