import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { AskUserInput } from "../../src/tools/agent-inputs";
import {
  askProblem,
  correctionText,
  matchAnswer,
  questionText,
} from "../../src/tools/ask-user";

// The shared ask_user vector (spec/conformance/vectors/ask-user-answers.json): the question
// rules, strict answers and channel messages, byte for byte as Python produces them.

const Vector = z.strictObject({
  description: z.string(),
  inputs: z.array(z.strictObject({ input: AskUserInput, valid: z.boolean() })),
  replies: z.array(
    z.strictObject({
      input: AskUserInput,
      reply: z.union([z.string(), z.array(z.string())]),
      answer: z.string().nullable(),
    }),
  ),
  messages: z.array(
    z.strictObject({
      input: AskUserInput,
      question: z.string(),
      correction: z.string(),
    }),
  ),
});
const vector = Vector.parse(
  JSON.parse(
    readFileSync(
      join(
        import.meta.dir,
        "../../../../../spec/conformance/vectors/ask-user-answers.json",
      ),
      "utf8",
    ),
  ),
);

describe("ask_user answers", () => {
  for (const c of vector.inputs)
    test(`${JSON.stringify(c.input)} is ${c.valid ? "askable" : "refused"}`, () => {
      expect(askProblem(c.input) === undefined).toBe(c.valid);
    });

  for (const c of vector.replies)
    test(`${JSON.stringify(c.reply)} to ${JSON.stringify(c.input.options)}`, () => {
      expect(matchAnswer(c.input, c.reply) ?? null).toBe(c.answer);
    });

  for (const c of vector.messages)
    test(`messages for ${JSON.stringify(c.input)}`, () => {
      expect(questionText(c.input)).toBe(c.question);
      expect(correctionText(c.input)).toBe(c.correction);
    });
});
