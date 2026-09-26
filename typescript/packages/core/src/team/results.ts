import type { z } from "zod";
import type {
  AskId,
  BudgetExceededData,
  MailId,
  MemberErrorCode as MemberErrorCodeSchema,
  MemberRef,
  ParkReason as ParkReasonSchema,
  ThreadId,
} from "../log";

// The results of the team tools ask, reply, wait and monitor (spec/api.json), in the TypeScript
// view: camelCase fields. The model sees each as its tool_result's preview, the Wire form of the
// same type: every key snake_case (askId is ask_id, timedOut is timed_out). The tool code builds
// its values as the Wire type, so the two can't drift.

type MemberErrorCode = z.infer<typeof MemberErrorCodeSchema>;
type ParkReason = z.infer<typeof ParkReasonSchema>;
type BudgetExceeded = z.infer<typeof BudgetExceededData>;

/** A camelCase key in snake_case: askId is ask_id. */
type Snake<S extends string> = S extends `${infer H}${infer R}`
  ? `${H extends Lowercase<H> ? H : `_${Lowercase<H>}`}${Snake<R>}`
  : S;

/**
 * A result as the model sees it in its tool_result: every key snake_case, all the way down, and
 * an id a plain string (JSON has no brands).
 */
export type Wire<T> = T extends z.core.$brand
  ? string
  : T extends string | number | boolean | null | undefined
    ? T
    : T extends readonly (infer E)[]
      ? readonly Wire<E>[]
      : {
          readonly [K in keyof T as K extends string ? Snake<K> : K]: Wire<
            T[K]
          >;
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

/**
 * Why an ask was refused: a send's refusals, no headroom for one request of its model, or lead
 * (an operator's ask addressed to the team's lead, which nothing here ever answers).
 */
export type AskRefusal = SendRefusal | "budget_exceeded" | "lead";

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

/** The ask tool's result; the model sees its Wire form (snake_case keys). */
export type AskResult =
  | AskOutcome
  | { readonly status: "refused"; readonly code: AskRefusal };

/** The reply tool's result; the model sees its Wire form (snake_case keys). */
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

/** The wait tool's result; the model sees its Wire form (snake_case keys). */
export type WaitResult =
  | Waited
  | { readonly status: "refused"; readonly code: ObserveRefusal };

/** Why a cancel was refused. */
export type CancelRefusal =
  | "forbidden"
  | "unknown_member"
  | "stale_member"
  | "member_ended";

/**
 * The cancel tool's result; the model sees its Wire form. cancel_requested: accepted and durable,
 * not yet applied; the member ends cancelled at its next step.
 */
export type CancelResult =
  | { readonly status: "cancel_requested"; readonly member: MemberRef }
  | { readonly status: "refused"; readonly code: CancelRefusal };

/** The monitor tool's result; the model sees its Wire form (snake_case keys). */
export type MonitorResult =
  | { readonly status: "monitoring"; readonly member: MemberRef }
  | { readonly status: "ended"; readonly result: MemberResult }
  | { readonly status: "refused"; readonly code: ObserveRefusal };
