import { z } from "zod";
import type { Opt, Strict } from "../log/zod-types";

// The built-in tool catalog: each tool's name, description and input schema,
// authored once here. `bun run schema:export` writes spec/schema/tools.v1.schema.json and the
// canonical catalog spec/schema/tools.v1.catalog.json (the exact bytes the model is shown);
// Python generates its models from them. No tool schema is written anywhere else.

type Def<T extends z.core.SomeType> = z.ZodDefault<T>;

const Path: z.ZodString = z
  .string()
  .min(1)
  .describe("Relative to /workspace, or absolute.");
const Dir: Def<z.ZodString> = z
  .string()
  .min(1)
  .default(".")
  .describe("Directory, relative to /workspace; . is the workspace root.");
const Expected: Opt<z.ZodString> = z
  .string()
  .regex(/^[0-9a-f]{64}$/)
  .describe(
    "The file's current lowercase hex sha256; a mismatch changes nothing.",
  )
  .optional();

export const BashInput: Strict<{
  command: z.ZodString;
  timeout_ms: Opt<z.ZodInt>;
}> = z.strictObject({
  command: z.string().min(1).describe("Run with bash -c."),
  timeout_ms: z.int().min(1).optional().describe("Default 120000."),
});

export const ReadInput: Strict<{
  path: z.ZodString;
  offset: Def<z.ZodInt>;
  limit: Def<z.ZodInt>;
}> = z.strictObject({
  path: Path,
  offset: z.int().min(1).default(1).describe("First line, 1-based."),
  limit: z.int().min(1).default(2000).describe("Lines to read."),
});

export const WriteInput: Strict<{
  path: z.ZodString;
  content: z.ZodString;
  expected_sha256: Opt<z.ZodString>;
}> = z.strictObject({
  path: Path,
  content: z.string(),
  expected_sha256: Expected,
});

export const EditInput: Strict<{
  path: z.ZodString;
  old_string: z.ZodString;
  new_string: z.ZodString;
  replace_all: Opt<z.ZodBoolean>;
  expected_sha256: Opt<z.ZodString>;
}> = z.strictObject({
  path: Path,
  old_string: z.string().min(1),
  new_string: z.string(),
  replace_all: z.boolean().optional(),
  expected_sha256: Expected,
});

export const LsInput: Strict<{ path: Def<z.ZodString> }> = z.strictObject({
  path: Dir,
});

export const GlobInput: Strict<{
  pattern: z.ZodString;
  path: Def<z.ZodString>;
}> = z.strictObject({
  pattern: z
    .string()
    .min(1)
    .describe("A gitignore-style glob relative to path; ** spans directories."),
  path: Dir,
});

export const GrepInput: Strict<{
  pattern: z.ZodString;
  path: Def<z.ZodString>;
  glob: Opt<z.ZodString>;
}> = z.strictObject({
  pattern: z.string().min(1).describe("An extended regular expression."),
  path: Dir,
  glob: z
    .string()
    .min(1)
    .optional()
    .describe("Only files whose path relative to path matches this glob."),
});

export const ReadToolResultInput: Strict<{
  call_id: z.ZodString;
  offset: z.ZodInt;
  length: z.ZodInt;
}> = z.strictObject({
  call_id: z.string().min(1),
  offset: z.int().min(0),
  length: z.int().min(1).max(65_536),
});

export type CatalogEntry = {
  readonly name: string;
  readonly description: string;
  readonly input: z.ZodType;
};

/** Sorted by name. */
export const CATALOG: readonly CatalogEntry[] = [
  {
    name: "bash",
    description:
      "Run a shell command in the sandbox. Returns exit_code, stdout and stderr previews, and full_output when truncated.",
    input: BashInput,
  },
  {
    name: "edit",
    description:
      "Replace old_string with new_string in a sandbox file. old_string must be unique unless replace_all.",
    input: EditInput,
  },
  {
    name: "glob",
    description:
      "List files under path whose path relative to it matches pattern.",
    input: GlobInput,
  },
  {
    name: "grep",
    description:
      "Search file contents under path with an extended regular expression; prints path:line:text.",
    input: GrepInput,
  },
  {
    name: "ls",
    description: "List the entries of a sandbox directory.",
    input: LsInput,
  },
  {
    name: "read",
    description: "Read a text file in the sandbox, with line numbers.",
    input: ReadInput,
  },
  {
    name: "read_tool_result",
    description:
      "Read bytes [offset, offset + length) of an earlier tool result by call_id, including spilled or cleared output.",
    input: ReadToolResultInput,
  },
  {
    name: "write",
    description: "Create or overwrite a file in the sandbox.",
    input: WriteInput,
  },
];

/** A catalog entry by name; the catalog is the only source of built-in schemas. */
export function entry(name: string): CatalogEntry {
  const found = CATALOG.find((e) => e.name === name);
  if (found === undefined) throw new Error(`no built-in ${name}`);
  return found;
}
