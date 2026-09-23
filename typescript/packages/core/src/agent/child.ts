import { z } from "zod";
import { assertNever } from "../assert-never";
import { canonicalize } from "../log";
import type { ChildEnd, Subagent } from "../loop";
import { reduce } from "../reduce";
import type { EventDraft } from "../store";
import type { ChildEnv, ChildFactory } from "./registry";
import type { RunResult } from "./result";
import { execute, type Resolved } from "./run";
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
      const inputs = child.inputs.map((text) => input(env, text));
      const result = await execute(
        asText,
        {
          store: env.store,
          principal: env.principal,
          child,
          ...(env.signal === undefined ? {} : { signal: env.signal }),
        },
        inputs,
      );
      return ended(result, await usage(env, result));
    },
  });
}

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
): Promise<ChildEnd["usage"]> {
  const { log } = await openStore(env.store);
  const read = log.read(result.thread.branch);
  if (!read.ok) return { input_tokens: null, output_tokens: null };
  const { usage: u } = reduce(read.value, log.now());
  const known = u.unknown_responses === 0;
  return {
    input_tokens: known ? u.input_tokens : null,
    output_tokens: known ? u.output_tokens : null,
  };
}

function ended(result: RunResult<string>, usage: ChildEnd["usage"]): ChildEnd {
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
      return { status: "failed", output: result.error.message, usage };
    case "parked":
      // ponytail: a child can't wait for an approval or answer yet; add a child park address.
      return {
        status: "failed",
        output: `parked: ${result.reason}`,
        usage,
      };
    case "handed_off":
      return { status: "failed", output: "handed off", usage };
    default:
      return assertNever(result);
  }
}
