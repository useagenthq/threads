import { z } from "zod";
import { assertNever } from "../assert-never";
import { canonicalize } from "../log";
import type { ChildDone, ChildEnd, ChildRun, Halt, Subagent } from "../loop";
import { reduce } from "../reduce";
import { type EventDraft, liveWriter } from "../store";
import { cancelTree } from "../thread/cancel";
import { execute } from "./execute";
import type { ChildEnv, ChildFactory } from "./registry";
import type { RunResult } from "./result";
import type { Plan, Resolved } from "./run";
import { openStore } from "./sqlite";

// An agent run as a subagent: a child thread in the same store, under the
// originating principal, with no sandbox (isolation none) and its parent's decisions as its
// ceiling. Its terminal result is what the parent records as agent_finished.

export function subagent<Deps, Output>(
  def: Resolved<Deps, Output>,
): ChildFactory {
  // The parent sees text: the final text, or the accepted value as canonical JSON.
  const asText: Resolved<Deps, string> = {
    ...def,
    decode: (text, accepted) => {
      if (accepted === undefined) return text;
      const json = canonicalize(z.json().parse(accepted));
      return json.ok ? json.value : text;
    },
  };
  return (env): Subagent => ({
    ...(def.budget === undefined ? {} : { budget: def.budget }),
    run: async (child) => {
      const { log } = await openStore(env.store);
      // A cancelled parent never starts a child; a started one gets its barrier and no input.
      if (child.cancel !== undefined) {
        if (!(await log.mainBranch(child.threadId)).ok) return CANCELLED;
        await cancelTree(log, child.threadId, child.cancel);
      }
      const inputs =
        child.cancel === undefined
          ? child.inputs.map((text) => input(env, text))
          : [];
      const result = await execute(asText, planOf(env, child), inputs);
      return ended(result, await usage(env, result));
    },
    held: async (child) => {
      const { log } = await openStore(env.store);
      const branch = await log.mainBranch(child);
      const writer = branch.ok ? liveWriter(branch.value) : undefined;
      return writer === undefined ? false : (await writer.fence()).ok;
    },
    stop: async (child, principal, reason) => {
      const { log } = await openStore(env.store);
      return cancelTree(log, child, principal, reason);
    },
  });
}

/** The child's run: its parent's store, principal, signal and (a live eval's) stubs. */
function planOf(env: ChildEnv, child: ChildRun): Plan<never> {
  return {
    store: env.store,
    principal: env.principal,
    child,
    ...(env.signal === undefined ? {} : { signal: env.signal }),
    ...(env.stub === undefined ? {} : { stub: env.stub }),
  };
}

const CANCELLED: ChildEnd = {
  status: "cancelled",
  output: "cancelled",
  usage: { input_tokens: null, output_tokens: null },
};

function input(env: ChildEnv, text: string): EventDraft {
  return {
    type: "user_input",
    type_version: 1,
    critical: true,
    actor: { kind: "host", principal: env.principal },
    data: { source: "parent_agent", text },
  };
}

/** The child's aggregate usage: a field is unknown (null) once any response's was. */
async function usage(
  env: ChildEnv,
  result: RunResult<string>,
): Promise<ChildDone["usage"]> {
  const { log } = await openStore(env.store);
  const read = await log.read(result.thread.branch);
  if (!read.ok) return { input_tokens: null, output_tokens: null };
  const { usage: u } = reduce(read.value, log.now());
  const known = u.unknown_responses === 0;
  return {
    input_tokens: known ? u.input_tokens : null,
    output_tokens: known ? u.output_tokens : null,
  };
}

function ended(
  result: RunResult<string>,
  usage: ChildDone["usage"],
): ChildEnd | Halt {
  switch (result.status) {
    case "completed":
      return { status: "completed", output: result.output, usage };
    case "budget_exhausted":
      return {
        status: "budget_exhausted",
        output: `budget exhausted: ${result.budget.limit}`,
        usage,
      };
    case "cancelled":
      return { status: "cancelled", output: "cancelled", usage };
    case "failed":
      // A held lease is another executor's turn, not the child's end.
      return result.error.code === "branch_busy"
        ? { code: "branch_busy", message: result.error.message }
        : { status: "failed", output: result.error.message, usage };
    case "parked":
      return { status: "parked", reason: result.reason };
    case "handed_off":
      return { status: "failed", output: "handed off", usage };
    default:
      return assertNever(result);
  }
}
