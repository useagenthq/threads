import { z } from "zod";
import type { Arr, Opt, Strict } from "../log/zod-types";

// Inputs of the memory and knowledge tools. Scope is never an argument: it comes
// from host config and the verified principal.

const K: Opt<z.ZodInt> = z
  .int()
  .min(1)
  .max(20)
  .optional()
  .describe("Most hits to return. Default 5.");

export const SaveMemoryInput: Strict<{ text: z.ZodString }> = z.strictObject({
  text: z.string().min(1).describe("The fact to remember, self-contained."),
});

export const SearchMemoryInput: Strict<{
  query: z.ZodString;
  k: Opt<z.ZodInt>;
}> = z.strictObject({
  query: z.string().min(1),
  k: K,
});

export const ForgetMemoryInput: Strict<{ id: z.ZodString }> = z.strictObject({
  id: z.string().min(1).describe("The id of a recalled memory."),
});

export const SearchKnowledgeInput: Strict<{
  query: z.ZodString;
  k: Opt<z.ZodInt>;
  sources: Opt<Arr<z.ZodString>>;
}> = z.strictObject({
  query: z.string().min(1),
  k: K,
  sources: z
    .array(z.string().min(1))
    .optional()
    .describe("Only these source ids."),
});
