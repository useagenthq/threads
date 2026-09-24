import { assertNever } from "@threads/core/host";
import type { RunOutcome } from "../outcome";
import { subagent } from "./ai-sdk";
import type { RunFacts } from "./facts";
import type { Chunk, Protocol } from "./frame";
import { answerable, interrupts } from "./interrupts";

// How a UI stream ends (spec/schema/ui/README.md, "Closing"): first whatever the log shows still
// open (a model step, a running legacy subagent) and the parts this connection opened live, then
// the run's outcome. A pure function of the log, like every other frame; none carries an id.

/** The ids the AG-UI run events echo: the client's on a POST, the threads ids on a GET. */
export type RunIds = { readonly threadId: string; readonly runId: string };

export function closing(
  protocol: Protocol,
  outcome: RunOutcome,
  facts: RunFacts,
  openLive: readonly string[],
  ids: RunIds,
): readonly Chunk[] {
  return [
    ...openState(protocol, outcome, facts),
    ...openLive.map((id) => textEnd(protocol, id)),
    ...(protocol === "ai-sdk"
      ? aiSdkEnd(outcome, facts)
      : agUiEnd(outcome, facts, ids)),
  ];
}

export function textEnd(protocol: Protocol, id: string): Chunk {
  return protocol === "ai-sdk"
    ? { type: "text-end", id }
    : { type: "TEXT_MESSAGE_END", messageId: id };
}

function openState(
  protocol: Protocol,
  outcome: RunOutcome,
  facts: RunFacts,
): readonly Chunk[] {
  const step: readonly Chunk[] =
    facts.openStep() === undefined
      ? []
      : [
          protocol === "ai-sdk"
            ? { type: "finish-step" }
            : { type: "STEP_FINISHED", stepName: "model" },
        ];
  const parked = outcome.status === "parked";
  const children = facts.running().map((s): Chunk => {
    if (protocol === "ai-sdk")
      return subagent(s.child, s.agent, parked ? "running" : "stopped");
    return parked
      ? {
          type: "SUBAGENT_FINISHED",
          subagentRunId: s.child,
          outcome: { type: "suspended" },
        }
      : {
          type: "SUBAGENT_ERROR",
          subagentRunId: s.child,
          message: `subagent ${s.agent} stopped: ${outcome.status}`,
        };
  });
  return [...step, ...children];
}

function aiSdkEnd(outcome: RunOutcome, facts: RunFacts): readonly Chunk[] {
  switch (outcome.status) {
    case "completed":
      return [
        {
          type: "finish",
          finishReason:
            facts.lastStop() === "refusal" ? "content-filter" : "stop",
        },
      ];
    case "parked":
      return [
        {
          type: "finish",
          finishReason: outcome.pending.every(answerable)
            ? "tool-calls"
            : "other",
        },
      ];
    case "cancelled":
      return [{ type: "abort", reason: "cancelled" }];
    case "failed":
    case "budget_exhausted":
    case "handed_off":
      return [
        { type: "error", errorText: failure(outcome) },
        {
          type: "finish",
          finishReason:
            outcome.status === "failed" && outcome.error.code === "max_output"
              ? "length"
              : "error",
        },
      ];
    default:
      return assertNever(outcome);
  }
}

function agUiEnd(
  outcome: RunOutcome,
  facts: RunFacts,
  ids: RunIds,
): readonly Chunk[] {
  const finished = { type: "RUN_FINISHED", ...ids };
  switch (outcome.status) {
    case "completed":
      return [
        {
          ...finished,
          outcome: { type: "success" },
          ...(outcome.output === null ? {} : { result: outcome.output }),
        },
      ];
    case "parked":
      return [
        {
          ...finished,
          outcome: {
            type: "interrupt",
            interrupts: interrupts(outcome.pending, facts),
          },
        },
      ];
    case "cancelled":
      return [{ ...finished, outcome: { type: "cancelled" } }];
    case "failed":
    case "budget_exhausted":
    case "handed_off":
      return [
        { type: "RUN_ERROR", message: failure(outcome), code: outcome.status },
      ];
    default:
      return assertNever(outcome);
  }
}

/** "<status>: <code>": the failure's code, the budget limit hit, or the handoff's thread. */
function failure(
  outcome: Extract<
    RunOutcome,
    { status: "failed" | "budget_exhausted" | "handed_off" }
  >,
): string {
  switch (outcome.status) {
    case "failed":
      return `failed: ${outcome.error.code}`;
    case "budget_exhausted":
      return `budget_exhausted: ${outcome.budget.limit}`;
    case "handed_off":
      return `handed_off: ${outcome.to_thread_id}`;
    default:
      return assertNever(outcome);
  }
}
