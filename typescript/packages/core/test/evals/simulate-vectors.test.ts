import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { judgeInput } from "../../src/evals/judge";
import { simulatedUserInput, userTurn } from "../../src/evals/simulated-user";
import { JsonValue, KnownEvent } from "../../src/log";

// spec/conformance/vectors (lane 32): the bytes the simulated user is sent, the output the runner
// accepts from it, and a simulated case's judge input, as both runtimes must compute them.

const VECTORS = join(
  import.meta.dir,
  "../../../../../spec/conformance/vectors",
);
const read = (name: string): unknown =>
  JSON.parse(readFileSync(join(VECTORS, name), "utf8"));

const UserInputVectors = z.object({
  vectors: z.array(
    z.object({
      name: z.string(),
      given: z.object({
        messages: z.array(
          z.object({ from: z.enum(["user", "agent"]), text: z.string() }),
        ),
      }),
      input: z.string(),
    }),
  ),
});

const TurnVectors = z.object({
  vectors: z.array(
    z.object({
      name: z.string(),
      output: z.unknown(),
      expect: z.enum(["accept", "simulator_invalid"]),
    }),
  ),
});

const ConversationVectors = z.object({
  vectors: z.array(
    z.object({
      name: z.string(),
      given: z.object({
        task: z.string(),
        events: z.array(KnownEvent),
        answer: JsonValue,
        rubric: z.array(z.string()),
        prefix_turns: z.number(),
        goal: z.string().optional(),
      }),
      input: z.string(),
    }),
  ),
});

describe("vectors/simulated-user-input.json", () => {
  for (const v of UserInputVectors.parse(read("simulated-user-input.json"))
    .vectors)
    test(v.name, () => {
      expect(simulatedUserInput(v.given.messages)).toBe(v.input);
    });
});

describe("vectors/user-turn.json", () => {
  for (const v of TurnVectors.parse(read("user-turn.json")).vectors)
    test(v.name, () => {
      const got = userTurn(v.output);
      expect(got.ok ? "accept" : got.error).toBe(v.expect);
    });
});

describe("vectors/judge-conversation-input.json", () => {
  for (const v of ConversationVectors.parse(
    read("judge-conversation-input.json"),
  ).vectors)
    test(v.name, () => {
      const { prefix_turns, goal, ...rest } = v.given;
      expect(
        judgeInput({
          ...rest,
          prefixTurns: prefix_turns,
          ...(goal === undefined ? {} : { goal }),
        }),
      ).toBe(v.input);
    });
});
