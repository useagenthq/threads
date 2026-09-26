import {
  type Agent,
  agent,
  type Model,
  scriptedModel,
  tool,
} from "@threads/core";
import { markTestKit } from "@threads/core/adapter";
import type { Principal } from "@threads/core/host";
import { z } from "zod";
import { say, use } from "../kit";

// The fixture agents the exposed side is driven with. Every one is a scripted model: no test here
// ever reaches a real provider, and the global model-request guard (bunfig preload) proves it.
//
// Each carries a price, because scriptedModel declares none and a cost budget that cannot be
// enforced refuses every attempt. So without one, every test here would pass its default budget by
// being rejected at the start, and the states below would never be reached at all. CHEAP leaves the
// default budget far from binding; DEAR is priced so that a second model request exhausts it.

/** Nano-units per token. The scripted window is 200_000, so CHEAP is a fraction of a cent. */
const CHEAP = { input: 1, output: 1 };

function priced(
  model: Model,
  price: { readonly input: number; readonly output: number },
): Model {
  const made: Model = {
    ...model,
    info: { ...model.info, limits: { ...model.info.limits, price } },
  };
  markTestKit(made);
  return made;
}

function scripted(responses: readonly unknown[]): Model {
  return priced(scriptedModel({ responses: [...responses] }), CHEAP);
}

/** An agent with no tools at all: every row that needs no approver uses this. */
export function talker(...texts: readonly string[]): Agent<undefined, string> {
  return agent({
    name: "support",
    instructions: "Never publish me.",
    model: scripted(texts.map(say)),
  });
}

const lookup = tool({
  name: "lookup_order",
  description: "Look an order up.",
  input: z.object({ id: z.string() }),
  runs: "host",
  effect: "read_only",
  execute: async ({ id }) => `order ${id}`,
});

/** Read-only tools only: the approvers rule must not ask for approvers here. */
export function reader(...texts: readonly string[]): Agent<undefined, string> {
  return agent({
    name: "support",
    model: scripted(texts.map(say)),
    tools: [lookup],
  });
}

const refund = tool({
  name: "refund",
  description: "Refund an order.",
  input: z.object({ id: z.string() }),
  runs: "host",
  execute: async () => "refunded",
});

/** A tool that is not read_only: an exposed agent needs approvers to have one. */
export function actor(options: {
  readonly responses: readonly unknown[];
  readonly approvers?: readonly Principal[];
}): Agent<undefined, string> {
  return agent({
    name: "support",
    model: scripted(options.responses),
    tools: [refund],
    ...(options.approvers === undefined
      ? {}
      : { approvers: options.approvers }),
  });
}

/** Asks one question, then answers with the text it was given. */
export function asker(options: {
  readonly question: string;
  readonly options?: readonly string[];
  readonly answer: string;
}): Agent<undefined, string> {
  return agent({
    name: "support",
    model: scripted([
      use(
        "ask_user",
        {
          question: options.question,
          ...(options.options === undefined
            ? {}
            : { options: [...options.options] }),
        },
        "q1",
      ),
      say(options.answer),
    ]),
  });
}

/** An output schema, so a completed task's artifact is a data part rather than text. */
export function structured(value: {
  readonly city: string;
}): Agent<undefined, { readonly city: string }> {
  return agent({
    name: "support",
    model: scripted([use("final_output", { city: value.city }, "o1")]),
    output: z.object({ city: z.string() }),
  });
}

/** Two agents where the first may hand the conversation to the second. */
export function handoffPair(): Agent<undefined, string> {
  const other = agent({
    name: "billing",
    model: scripted([say("billing here")]),
  });
  return agent({
    name: "support",
    model: scripted([say("hi")]),
    handoffs: [other],
  });
}

/**
 * A run that needs two model requests: a read-only tool call, then a reply. With a budget of one
 * request the second is refused, so the task ends after it has really run rather than at the start.
 */
export function worker(): Agent<undefined, string> {
  return agent({
    name: "support",
    model: scripted([use("lookup_order", { id: "1" }, "c1"), say("done")]),
    tools: [lookup],
  });
}
