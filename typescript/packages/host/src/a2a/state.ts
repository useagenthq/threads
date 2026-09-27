import type {
  Artifact,
  Message,
  Task,
  TaskState,
  TaskStatus,
} from "@threads/a2a/protocol";
import { assertNever, type EventId, type KnownEvent } from "@threads/core/host";
import type { RunOutcome } from "../outcome";
import { artifactId, statusMessageId } from "./keys";
import { askText, type OpenAsk } from "./question";

// A committed run slice as an A2A Task. A pure function of the events: the same log always
// renders the same task, which is what lets a stream resume, a GetTask after a stream and two
// hosts all answer the same thing. The outcome comes from the host's own run-slice readers, so
// there is no second reducer to keep in step.

type ParkedReason = Extract<RunOutcome, { status: "parked" }>["reason"];

export type Slice = {
  readonly taskId: EventId;
  readonly contextId: string;
  /** The run's own events, from its user_input to where the run ends or the log stops. */
  readonly own: readonly KnownEvent[];
  /** outcomeFromLog over the same prefix: undefined while the run is still going. */
  readonly outcome: RunOutcome | undefined;
  /** The question open at the end of this prefix, so a replayed frame shows the right one. */
  readonly question: OpenAsk | undefined;
};

export function taskOf(slice: Slice): Task {
  const { state, text } = stateOf(slice);
  const seq = slice.own.at(-1)?.seq ?? 0;
  const time = slice.own.at(-1)?.time;
  const status: TaskStatus = {
    state,
    ...(text === undefined ? {} : { message: statusMessage(slice, seq, text) }),
    ...(time === undefined ? {} : { timestamp: new Date(time).toISOString() }),
  };
  const artifacts = artifactsOf(slice);
  return {
    id: slice.taskId,
    contextId: slice.contextId,
    status,
    ...(artifacts === undefined ? {} : { artifacts: [...artifacts] }),
  };
}

/** The task's state, and the status message's text when the state carries one. */
function stateOf(slice: Slice): {
  readonly state: TaskState;
  readonly text: string | undefined;
} {
  const { outcome } = slice;
  if (outcome === undefined)
    return {
      // A run whose input is recorded but which has not asked the model anything yet has been
      // accepted and nothing more: submitted, not working.
      state: ran(slice) ? "TASK_STATE_WORKING" : "TASK_STATE_SUBMITTED",
      text: undefined,
    };
  switch (outcome.status) {
    case "completed":
      return { state: "TASK_STATE_COMPLETED", text: undefined };
    case "cancelled":
      return { state: "TASK_STATE_CANCELED", text: undefined };
    case "failed":
      return ended(slice, `${outcome.error.code}: ${outcome.error.message}`);
    case "budget_exhausted":
      return ended(
        slice,
        `budget_exhausted: ${outcome.budget.scope} budget ${outcome.budget.limit} of ${outcome.budget.limit_value} reached`,
      );
    case "parked":
      return parked(slice, outcome.reason);
    case "handed_off":
      // An agent with handoffs cannot be exposed, so this is unreachable by construction.
      return {
        state: "TASK_STATE_FAILED",
        text: "handed_off: an exposed agent cannot hand off",
      };
    default:
      return assertNever(outcome);
  }
}

/**
 * A run that ended badly: failed after it ran, rejected when it never got as far as a model
 * request (a policy refusal, the host ceiling, or a budget already spent at the start).
 */
function ended(
  slice: Slice,
  text: string,
): { readonly state: TaskState; readonly text: string } {
  return {
    state: ran(slice) ? "TASK_STATE_FAILED" : "TASK_STATE_REJECTED",
    text,
  };
}

function ran(slice: Slice): boolean {
  return slice.own.some((e) => e.type === "model_request");
}

/**
 * A park as a caller sees it. Only a question is INPUT_REQUIRED: an approval and an uncertain
 * effect show WORKING on purpose, because neither can be decided over A2A and neither means the
 * work failed. We do not know, so we say working, and a person resolves it through threads.
 */
function parked(
  slice: Slice,
  reason: ParkedReason,
): { readonly state: TaskState; readonly text: string } {
  switch (reason) {
    case "awaiting_input": {
      const question = slice.question;
      return {
        state: "TASK_STATE_INPUT_REQUIRED",
        text:
          question === undefined ? "a question is open" : askText(question.ask),
      };
    }
    case "awaiting_approval":
      return { state: "TASK_STATE_WORKING", text: "waiting for approval" };
    case "effect_unknown":
      return {
        state: "TASK_STATE_WORKING",
        text: "waiting for a person to resolve an uncertain action",
      };
    // Waiting on something inside this host, which a caller can neither see nor resolve. Working
    // is the honest answer, and the reason is the most we can say without leaking what it is.
    case "awaiting_resource":
    case "awaiting_member":
      return { state: "TASK_STATE_WORKING", text: reason };
    default:
      return assertNever(reason);
  }
}

/** A status message: role agent, the task's own ids, a derived id and one text part. */
function statusMessage(slice: Slice, seq: number, text: string): Message {
  return {
    messageId: statusMessageId(slice.taskId, seq),
    contextId: slice.contextId,
    taskId: slice.taskId,
    role: "ROLE_AGENT",
    parts: [{ text }],
  };
}

/**
 * A completed run's one artifact: a text part, or a data part when the agent has an output
 * schema, which is exactly when its output is not a bare string.
 */
export function artifactsOf(slice: Slice): readonly Artifact[] | undefined {
  const { outcome } = slice;
  if (outcome?.status !== "completed") return undefined;
  const part =
    typeof outcome.output === "string"
      ? { text: outcome.output }
      : { data: outcome.output };
  return [
    {
      artifactId: artifactId(slice.taskId),
      name: "output",
      parts: [part],
    },
  ];
}
