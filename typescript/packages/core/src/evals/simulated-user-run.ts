import type { z } from "zod";
import { agent } from "../agent/agent";
import type { Budget } from "../log";
import type { Model } from "../model";
import { assertModelAllowed, ModelBlockedError } from "../model/guard";
import type { Thread } from "../thread";
import type { CaseSimulate } from "./files";
import { type Env, EVAL_PRINCIPAL, guarded, unfinished } from "./live-env";
import { type Simulation, UserTurn } from "./schema";
import {
  simulatedUserInput,
  userInstructions,
  userTurn,
  type VisibleMessage,
} from "./simulated-user";
import { treeTotals } from "./tree-totals";

// Who plays the user of a simulated case (spec lane 32, B.2.4 and C): fixed messages, or a
// threads agent on its own thread, so every user turn is on the log. Both answer the same
// question -- what does the user say next -- and both end the conversation as a value.

export type UserPlay =
  | { readonly kind: "message"; readonly text: string }
  | { readonly kind: "ended"; readonly ending: Simulation["ended"] }
  | { readonly kind: "error"; readonly reason: string }
  | { readonly kind: "blocked"; readonly model: string };

export type UserPlayer = {
  /** The next message, given what the agent said since the player's last reply. */
  readonly next: (
    seen: readonly VisibleMessage[],
    budget: z.infer<typeof Budget>,
  ) => Promise<UserPlay>;
  /** The player's thread, once it has one; a script user never has one. */
  readonly thread: () => Thread | undefined;
  readonly calls: () => Promise<number>;
};

/** Fixed messages, sent in order after the opener: no user model is needed. */
export function scriptPlayer(messages: readonly string[]): UserPlayer {
  let at = 0;
  return {
    next: async () => {
      const text = messages[at];
      at += 1;
      return text === undefined
        ? { kind: "ended", ending: "script_done" }
        : { kind: "message", text };
    },
    thread: () => undefined,
    calls: async () => 0,
  };
}

/** A model playing the persona and goal, on its own thread (tenant evals). */
export function modelPlayer(
  simulate: Extract<CaseSimulate, { kind: "model" }>,
  model: Model,
  env: Env,
): UserPlayer {
  const user = agent({
    name: "simulated_user",
    model,
    instructions: userInstructions(simulate.persona, simulate.goal),
    output: UserTurn,
    outputRetries: 1,
  });
  let thread: Thread | undefined;
  return {
    next: async (seen, budget) => {
      const ran = await guarded(() =>
        user.run(simulatedUserInput(seen), {
          store: env.store,
          principal: EVAL_PRINCIPAL,
          budget,
          ...(thread === undefined ? {} : { thread }),
        }),
      );
      if ("blocked" in ran) return { kind: "blocked", model: ran.blocked };
      if ("config" in ran) return { kind: "error", reason: ran.config };
      thread = ran.thread;
      if (ran.status === "failed")
        return { kind: "error", reason: "simulator_invalid" };
      if (ran.status !== "completed")
        return { kind: "error", reason: unfinished(ran) };
      const turn = userTurn(ran.output);
      if (!turn.ok) return { kind: "error", reason: turn.error };
      return turn.value.done
        ? { kind: "ended", ending: "user_done" }
        : { kind: "message", text: turn.value.message };
    },
    thread: () => thread,
    calls: async () =>
      thread === undefined ? 0 : (await treeTotals(env.store, thread)).requests,
  };
}

/**
 * The player for a case, or the guard's block: a model user is checked before the first agent
 * run, so a blocked user model dispatches nothing at all.
 */
export function playerFor(
  simulate: CaseSimulate,
  env: Env,
): UserPlayer | { readonly blocked: string } {
  if (simulate.kind === "script") return scriptPlayer(simulate.messages);
  const model = env.live.user;
  if (model === undefined)
    throw new Error(
      "a model-kind case is checked for live.user before it runs",
    );
  try {
    assertModelAllowed(model);
  } catch (error) {
    if (error instanceof ModelBlockedError) return { blocked: error.model };
    throw error;
  }
  return modelPlayer(simulate, model, env);
}
