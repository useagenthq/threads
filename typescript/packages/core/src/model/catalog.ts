import { z } from "zod";
import type { Arr, Lit, Opt, Strict } from "../log/zod-types";
import type { Result } from "../result";
import { err, ok } from "../result";

// A provider's model catalog, spec/models/<provider>.v1.json: the limits of exact model ids, each
// read from the provider's own source on the day it names. Exported to
// spec/schema/model-catalog.v1.schema.json; Python's model is generated from that file.

const Count: z.ZodInt = z.int().min(1).max(Number.MAX_SAFE_INTEGER);
const Text: z.ZodString = z.string().min(1);

type Limits = {
  max_input_tokens: typeof Count;
  max_output_tokens: typeof Count;
};
const LIMITS: Limits = {
  max_input_tokens: Count.describe(
    "The most input tokens one request may carry. Pinned as policy.models[].context_window.",
  ),
  max_output_tokens: Count.describe(
    "The most output tokens one response may hold.",
  ),
};

export const CatalogEntry: Strict<
  Limits & {
    id: typeof Text;
    alias_of: Opt<typeof Text>;
    source: z.ZodString;
    note: Opt<typeof Text>;
    verified: z.ZodString;
  }
> = z.strictObject({
  id: Text.describe(
    "The provider's exact model id; matched by string equality only.",
  ),
  ...LIMITS,
  alias_of: Text.optional().describe(
    "Informational: the dated snapshot this alias pointed to when it was verified.",
  ),
  source: z
    .string()
    .regex(/^https:\/\/\S+$/)
    .describe("Where the limits were read (a Models API URL or a model page)."),
  note: Text.optional().describe(
    "How a value was obtained when the source doesn't state it outright (evidence, not behavior).",
  ),
  verified: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .describe("The day the limits were last read there (YYYY-MM-DD)."),
});

export const Withdrawal: Strict<{
  id: typeof Text;
  reason: typeof Text;
  use: Strict<Limits>;
}> = z.strictObject({
  id: Text.describe(
    "A released entry that turned out wrong. It stays in entries.",
  ),
  reason: Text,
  use: z.strictObject(LIMITS).describe("The corrected limits."),
});

export const ModelCatalog: Strict<{
  version: Lit<1>;
  provider: z.ZodString;
  entries: Arr<typeof CatalogEntry>;
  withdrawn: Arr<typeof Withdrawal>;
}> = z
  .strictObject({
    version: z.literal(1),
    provider: z.string().regex(/^[a-z][a-z0-9_]{0,63}$/),
    entries: z.array(CatalogEntry),
    withdrawn: z.array(Withdrawal),
  })
  .describe(
    "Entry ids are unique, and so are withdrawn ids (a semantic rule, spec/schema/README.md). A released entry's id, limits and alias_of never change; a wrong one is withdrawn.",
  );

export type ModelCatalog = z.infer<typeof ModelCatalog>;

/** One catalog file: the schema, then the rule JSON Schema can't state (unique ids). */
export function parseCatalog(text: string): Result<ModelCatalog, string> {
  let json: unknown;
  try {
    json = JSON.parse(text);
  } catch (error) {
    return err(`not JSON: ${String(error)}`);
  }
  const parsed = ModelCatalog.safeParse(json);
  if (!parsed.success) return err(z.prettifyError(parsed.error));
  const repeated =
    duplicate(parsed.data.entries.map((e) => e.id)) ??
    duplicate(parsed.data.withdrawn.map((w) => w.id));
  return repeated === undefined
    ? ok(parsed.data)
    : err(`model id "${repeated}" is listed twice`);
}

function duplicate(ids: readonly string[]): string | undefined {
  return ids.find((id, i) => ids.indexOf(id) !== i);
}
