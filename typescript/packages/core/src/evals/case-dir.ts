import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { KnownEvent } from "../log";
import type { Lit, Opt, Strict } from "../log/zod-types";
import { scriptProblem } from "../model/scripted";
import { err, ok, type Result } from "../result";
import { SandboxScript } from "../sandbox/script";
import type { EventDraft } from "../store";
import {
  CaseLine0,
  CaseOffline,
  CaseSnapshot,
  ExtensionScript,
  SandboxResults,
} from "./files";
import { EventMatcher, Rubric } from "./schema";

// One saved case directory, read and parsed at the boundary (spec/conformance/case.schema.json,
// spec/schema/eval.v1.schema.json). The case's files are user input: anything malformed makes
// the case an `error`, never a throw.

const IMPL = "threads-ts";

type Matchers = z.ZodArray<typeof EventMatcher>;
const CaseFile: Strict<{
  name: z.ZodString;
  family: z.ZodString;
  kind: Lit<"stub">;
  description: z.ZodString;
  clock: Strict<{ now: z.ZodInt }>;
  model_script: Opt<Lit<"model.json">>;
  sandbox_script: Opt<Lit<"sandbox.json">>;
  stub_script: Opt<Lit<"stubs.json">>;
  extension_script: Opt<Lit<"extensions.json">>;
  input: Opt<Strict<{ text: Opt<z.ZodString> }>>;
  expect: Strict<{ must: Matchers; expect: Opt<Matchers> }>;
  rubric: Opt<typeof Rubric>;
  snapshot: Opt<typeof CaseSnapshot>;
  offline: Opt<typeof CaseOffline>;
  line0: Opt<typeof CaseLine0>;
}> = z.strictObject({
  name: z.string(),
  family: z.string(),
  kind: z.literal("stub"),
  description: z.string(),
  clock: z.strictObject({ now: z.int().min(0) }),
  model_script: z.literal("model.json").optional(),
  sandbox_script: z.literal("sandbox.json").optional(),
  stub_script: z.literal("stubs.json").optional(),
  extension_script: z.literal("extensions.json").optional(),
  input: z.strictObject({ text: z.string().optional() }).optional(),
  expect: z.strictObject({
    must: z.array(EventMatcher).min(1),
    expect: z.array(EventMatcher).optional(),
  }),
  rubric: Rubric.optional(),
  snapshot: CaseSnapshot.optional(),
  offline: CaseOffline.optional(),
  line0: CaseLine0.optional(),
});
export type CaseFile = z.infer<typeof CaseFile>;

/** expected.<impl>.json as the runner reads it: the recorded turn, or old-format matchers. */
const Expected = z.looseObject({
  appended: z.array(z.unknown()).optional(),
});

export type CaseDir = {
  readonly name: string;
  readonly meta: CaseFile;
  readonly log: Uint8Array;
  readonly artifacts: readonly Uint8Array[];
  readonly model: unknown;
  readonly sandbox: SandboxResults | SandboxScript | undefined;
  readonly stubs: unknown;
  readonly extensions: ExtensionScript | undefined;
  readonly line0: Uint8Array | undefined;
  /** The recorded turn, in full; undefined for a case saved before full events were kept. */
  readonly recorded: readonly KnownEvent[] | undefined;
  /**
   * An older case's `appended` matchers; undefined when it has none (a Python save_case before
   * this lane wrote only the outcome and state), so only its `must` and scripts are checked.
   */
  readonly matchers: readonly z.infer<typeof EventMatcher>[] | undefined;
};

type Read = Result<unknown, string>;

function json(dir: string, file: string): Read {
  const path = join(dir, file);
  if (!existsSync(path)) return err(`${file} is missing`);
  try {
    return ok(JSON.parse(readFileSync(path, "utf8")));
  } catch (error) {
    return err(
      `${file}: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
}

function parsed<T>(
  read: Read,
  schema: z.ZodType<T>,
  file: string,
): Result<T, string> {
  if (!read.ok) return read;
  const got = schema.safeParse(read.value);
  return got.success
    ? ok(got.data)
    : err(`${file}: ${got.error.issues[0]?.message ?? "invalid"}`);
}

/** sandbox.json: v2 results, else the v1 shape (one entry per tool name). */
function sandboxOf(
  dir: string,
  meta: CaseFile,
): Result<CaseDir["sandbox"], string> {
  if (meta.sandbox_script === undefined) return ok(undefined);
  const read = json(dir, "sandbox.json");
  if (!read.ok) return read;
  const v2 = SandboxResults.safeParse(read.value);
  if (v2.success) return ok(v2.data);
  return parsed(read, SandboxScript, "sandbox.json");
}

function bytes(path: string): Uint8Array | undefined {
  return existsSync(path) ? new Uint8Array(readFileSync(path)) : undefined;
}

/** The recorded turn: full events, or the matchers an older saveCase wrote. */
function appendedOf(
  read: Read,
): Result<Pick<CaseDir, "recorded" | "matchers">, string> {
  const expected = parsed(read, Expected, "expected.json");
  if (!expected.ok) return expected;
  const items = expected.value.appended;
  if (items === undefined)
    return ok({ recorded: undefined, matchers: undefined });
  const events = z.array(KnownEvent).safeParse(items);
  if (events.success && items.length > 0)
    return ok({ recorded: events.data, matchers: [] });
  const matchers = z.array(EventMatcher).safeParse(items);
  return matchers.success
    ? ok({ recorded: undefined, matchers: matchers.data })
    : err("expected.json: appended holds neither events nor matchers");
}

/** A file saveCase writes once per implementation: this one's name, else the shared one. */
function own(dir: string, name: string): string {
  const dot = name.indexOf(".");
  const mine = `${name.slice(0, dot)}.${IMPL}${name.slice(dot)}`;
  return existsSync(join(dir, mine)) ? mine : name;
}

export function readCase(root: string, name: string): Result<CaseDir, string> {
  const dir = join(root, name);
  const meta = parsed(json(dir, "case.json"), CaseFile, "case.json");
  if (!meta.ok) return meta;
  const log = bytes(join(dir, own(dir, "log.jsonl")));
  if (log === undefined) return err("log.jsonl is missing");
  const model =
    meta.value.model_script === undefined
      ? ok(undefined)
      : json(dir, "model.json");
  if (!model.ok) return model;
  const problem =
    model.value === undefined ? undefined : scriptProblem(model.value);
  if (problem !== undefined) return err(`model.json: ${problem}`);
  const sandbox = sandboxOf(dir, meta.value);
  if (!sandbox.ok) return sandbox;
  const stubs =
    meta.value.stub_script === undefined
      ? ok(undefined)
      : json(dir, "stubs.json");
  if (!stubs.ok) return stubs;
  const extensions =
    meta.value.extension_script === undefined
      ? ok(undefined)
      : parsed(
          json(dir, "extensions.json"),
          ExtensionScript,
          "extensions.json",
        );
  if (!extensions.ok) return extensions;
  const appended = appendedOf(json(dir, own(dir, "expected.json")));
  if (!appended.ok) return appended;
  const artifactDir = join(dir, "artifacts");
  return ok({
    name,
    meta: meta.value,
    log,
    artifacts: existsSync(artifactDir)
      ? readdirSync(artifactDir).map(
          (f) => new Uint8Array(readFileSync(join(artifactDir, f))),
        )
      : [],
    model: model.value,
    sandbox: sandbox.value,
    stubs: stubs.value,
    extensions: extensions.value,
    line0: bytes(join(dir, "line0.json")),
    ...appended.value,
  });
}

/** What the rerun sends: the recorded user_input as it was appended, or the case's text. */
export function inputOf(c: CaseDir): EventDraft | undefined {
  const recorded = c.recorded?.find((e) => e.type === "user_input");
  if (recorded?.type === "user_input") {
    const { type, type_version, critical, actor, data } = recorded;
    return { type, type_version, critical, actor, data };
  }
  const text = c.meta.input?.text;
  return text === undefined
    ? undefined
    : {
        type: "user_input",
        type_version: 1,
        critical: true,
        actor: {
          kind: "user",
          principal: { issuer: "api", tenant: "local", subject: "operator" },
        },
        data: { source: "api", text },
      };
}

/** Every case directory under `root`, by name. */
export function caseNames(root: string): readonly string[] {
  if (!existsSync(root)) return [];
  return readdirSync(root, { withFileTypes: true })
    .filter(
      (d) => d.isDirectory() && existsSync(join(root, d.name, "case.json")),
    )
    .map((d) => d.name)
    .toSorted();
}
