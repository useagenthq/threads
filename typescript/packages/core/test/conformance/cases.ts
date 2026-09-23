import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { ErrorCode } from "../../src/log";
import { type Fixture, fixture } from "../store/helpers";

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
  appended: z
    .array(
      z.strictObject({
        type: z.string(),
        seq: z.int().optional(),
        actor_kind: z.string().optional(),
        epoch: z.int().optional(),
        branch_id: z.string().optional(),
        critical: z.boolean().optional(),
        data: z.record(z.string(), z.unknown()).optional(),
      }),
    )
    .optional(),
  sandbox: z
    .partialRecord(
      z.enum(["dispatches", "new_executions", "lookups"]),
      z.record(z.string(), z.int()),
    )
    .optional(),
  fork: z
    .strictObject({
      child_created: z.boolean(),
      at_seq: z.int().optional(),
      parent_unchanged: z.boolean(),
      knowledge_revision: z.int().nullable().optional(),
    })
    .optional(),
  resources: z
    .strictObject({
      creates: z.int(),
      rows: z.array(z.strictObject({ kind: z.string(), state: z.string() })),
    })
    .optional(),
  render: z
    .strictObject({
      next_request_sha256: z.string(),
      declared_prefix: z.strictObject({ bytes: z.int(), sha256: z.string() }),
    })
    .optional(),
  stubs: z.strictObject({ consumed: z.int(), unmatched: z.int() }).optional(),
  responses: z.unknown().optional(),
  inbox: z.unknown().optional(),
  decisions: z.unknown().optional(),
});

/** An EventMatcher of case.schema.json: data is a deep subset. */
export type Matcher = {
  readonly type: string;
  readonly seq?: number | undefined;
  readonly actor_kind?: string | undefined;
  readonly epoch?: number | undefined;
  readonly branch_id?: string | undefined;
  readonly critical?: boolean | undefined;
  readonly data?: Readonly<Record<string, unknown>> | undefined;
};

export type Counters = Partial<
  Record<
    "dispatches" | "new_executions" | "lookups",
    Readonly<Record<string, number>>
  >
>;

/** What the runner reads from one case directory. */
export type Case = {
  readonly dir: string;
  /** case.json input, parsed by the kind's runner. */
  readonly input: unknown;
  readonly fork:
    | {
        readonly child_created: boolean;
        readonly at_seq?: number | undefined;
        readonly parent_unchanged: boolean;
        readonly knowledge_revision?: number | null | undefined;
      }
    | undefined;
  readonly resources:
    | {
        readonly creates: number;
        readonly rows: readonly {
          readonly kind: string;
          readonly state: string;
        }[];
      }
    | undefined;
  readonly appended: readonly Matcher[] | undefined;
  readonly sandbox: Counters | undefined;
  readonly stubs:
    | { readonly consumed: number; readonly unmatched: number }
    | undefined;
  /** Parsed script files, by name; the test kit parses their shape. */
  readonly scripts: {
    readonly model: unknown;
    readonly sandbox: unknown;
    readonly stubs: unknown;
  };
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
  /** artifacts/<sha256>: content-addressed bytes the log references. */
  readonly artifacts: readonly Uint8Array[];
  /** request.bytes: the next Render v1 request (render kind). */
  readonly request: Uint8Array | undefined;
  readonly render:
    | {
        readonly next_request_sha256: string;
        readonly declared_prefix: {
          readonly bytes: number;
          readonly sha256: string;
        };
      }
    | undefined;
};

const IMPL = "threads-ts";

/** A recover case ships one file per writer; this runner reads its own. */
export function ownFile(dir: string, name: string): string {
  const dot = name.indexOf(".");
  const mine = join(dir, `${name.slice(0, dot)}.${IMPL}${name.slice(dot)}`);
  return existsSync(mine) ? mine : join(dir, name);
}

function readBytes(path: string): Uint8Array | undefined {
  return existsSync(path) ? new Uint8Array(readFileSync(path)) : undefined;
}

function readArtifacts(dir: string): readonly Uint8Array[] {
  const root = join(dir, "artifacts");
  if (!existsSync(root)) return [];
  return readdirSync(root).map(
    (name) => new Uint8Array(readFileSync(join(root, name))),
  );
}

function readJson(path: string): unknown {
  return JSON.parse(readFileSync(path, "utf8"));
}

export function loadCase(name: string): Case {
  const dir = join(CASES_DIR, name);
  const meta = CaseFile.parse(readJson(join(dir, "case.json")));
  const expected = ExpectedFile.parse(readJson(ownFile(dir, "expected.json")));
  const logPath = ownFile(dir, "log.jsonl");
  const script = (file: string | undefined): unknown =>
    file === undefined ? undefined : readJson(join(dir, file));
  return {
    dir,
    input: meta.input,
    fork: expected.fork,
    resources: expected.resources,
    appended: expected.appended,
    sandbox: expected.sandbox,
    stubs: expected.stubs,
    scripts: {
      model: script(meta.model_script),
      sandbox: script(meta.sandbox_script),
      stubs: script(meta.stub_script),
    },
    name,
    kind: meta.kind,
    now: meta.clock.now,
    error: expected.error,
    state: expected.state,
    projections: expected.projections,
    committedBytes: expected.committed_bytes,
    headVerified: expected.head_verified ?? true,
    log: readBytes(logPath),
    artifacts: readArtifacts(dir),
    request: readBytes(join(dir, "request.bytes")),
    render: expected.render,
  };
}

export const CASE_NAMES: readonly string[] = readdirSync(CASES_DIR).toSorted();

/** Plain JSON of a value, so comparisons ignore absent optionals and class identity. */
export function plain(value: unknown): unknown {
  return JSON.parse(JSON.stringify(value));
}

/** A fresh store that already holds the case's artifacts, as an import expects. */
export function caseStore(c: Case): Fixture {
  const f = fixture();
  for (const artifact of c.artifacts) f.artifacts.put(artifact);
  return f;
}
