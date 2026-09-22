import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { ErrorCode } from "../../src/log";

// Loads spec/conformance/cases. Fixture files are parsed strictly: an unknown key, or an
// unknown kind, fails the case instead of being ignored (spec/conformance/README.md).

export const CASES_DIR: string = join(
  import.meta.dir,
  "../../../../../spec/conformance/cases",
);

const KINDS = [
  "reduce",
  "render",
  "recover",
  "fork",
  "stub",
  "intake",
  "security",
  "parity",
  "policy",
] as const;
export type Kind = (typeof KINDS)[number];

const CaseFile = z.strictObject({
  name: z.string(),
  family: z.string(),
  kind: z.enum(KINDS),
  description: z.string(),
  clock: z.strictObject({ now: z.int().min(0) }),
  model_script: z.literal("model.json").optional(),
  sandbox_script: z.literal("sandbox.json").optional(),
  stub_script: z.literal("stubs.json").optional(),
  input: z.record(z.string(), z.unknown()).optional(),
});

// Keys this runner compares are typed; keys owned by later runners are only admitted.
const ExpectedFile = z.strictObject({
  outcome: z.enum(["ok", "error"]),
  error: z
    .strictObject({ code: ErrorCode, seq: z.int().min(0).optional() })
    .optional(),
  state: z.record(z.string(), z.unknown()).optional(),
  committed_bytes: z.int().min(0).optional(),
  head_verified: z.boolean().optional(),
  projections: z.record(z.string(), z.unknown()).optional(),
  appended: z.array(z.unknown()).optional(),
  sandbox: z.unknown().optional(),
  fork: z.unknown().optional(),
  resources: z.unknown().optional(),
  render: z.unknown().optional(),
  stubs: z.unknown().optional(),
  responses: z.unknown().optional(),
  inbox: z.unknown().optional(),
  decisions: z.unknown().optional(),
});

/** What the runner reads from one case directory. */
export type Case = {
  readonly name: string;
  readonly kind: Kind;
  readonly now: number;
  readonly error:
    | { readonly code: string; readonly seq?: number | undefined }
    | undefined;
  readonly state: unknown;
  readonly projections: Readonly<Record<string, unknown>> | undefined;
  readonly committedBytes: number | undefined;
  readonly headVerified: boolean;
  readonly log: Uint8Array | undefined;
};

const IMPL = "threads-ts";

/** A recover case ships one file per writer; this runner reads its own. */
export function ownFile(dir: string, name: string): string {
  const dot = name.indexOf(".");
  const mine = join(dir, `${name.slice(0, dot)}.${IMPL}${name.slice(dot)}`);
  return existsSync(mine) ? mine : join(dir, name);
}

function readJson(path: string): unknown {
  return JSON.parse(readFileSync(path, "utf8"));
}

export function loadCase(name: string): Case {
  const dir = join(CASES_DIR, name);
  const meta = CaseFile.parse(readJson(join(dir, "case.json")));
  const expected = ExpectedFile.parse(readJson(ownFile(dir, "expected.json")));
  const logPath = ownFile(dir, "log.jsonl");
  return {
    name,
    kind: meta.kind,
    now: meta.clock.now,
    error: expected.error,
    state: expected.state,
    projections: expected.projections,
    committedBytes: expected.committed_bytes,
    headVerified: expected.head_verified ?? true,
    log: existsSync(logPath)
      ? new Uint8Array(readFileSync(logPath))
      : undefined,
  };
}

export const CASE_NAMES: readonly string[] = readdirSync(CASES_DIR).toSorted();

/** Plain JSON of a value, so comparisons ignore absent optionals and class identity. */
export function plain(value: unknown): unknown {
  return JSON.parse(JSON.stringify(value));
}
