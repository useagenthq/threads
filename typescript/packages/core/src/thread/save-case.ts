import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import {
  ArtifactRef,
  type BranchId,
  type EventId,
  type KnownEvent,
} from "../log";
import { knownEvents, reduce } from "../reduce";
import { err, ok, type Result } from "../result";
import type { Sandbox } from "../sandbox/protocol";
import type { ArtifactStore, LogStore } from "../store";
import { type VerifiedLog, verifyExport } from "../verify";
import { type LogError, logError } from "../verify/error";
import { matches, modelScript, sandboxScript, stubScript } from "./case-files";
import { caseLog, type Impl } from "./case-log";
import type { ForkPoint } from "./open";

// saveCase() (spec/api.json, ): a stub-kind conformance case in exactly the
// spec/conformance layout, so the same runners replay it. The log is the chain through the
// snapshot; input.text is the next user_input after it; the recorded turn that followed becomes
// model.json, stubs.json (mediated calls), sandbox.json (read-only calls) and `appended`.

/** case.schema.json EventMatcher: exact on the listed envelope keys, deep subset on data. */
export type EventMatcher = {
  readonly type: string;
  readonly seq?: number;
  readonly actor_kind?: string;
  readonly epoch?: number;
  readonly branch_id?: string;
  readonly critical?: boolean;
  readonly data?: Readonly<Record<string, unknown>>;
};

/** api.json CaseExpectation: `must` fails the case when unmatched; `expect` is only reported. */
export type CaseExpectation = {
  readonly must: readonly EventMatcher[];
  readonly expect?: readonly EventMatcher[];
};

export type SaveCaseOptions = {
  readonly expect: CaseExpectation;
  /** The effects policy is required; stub is the only one. */
  readonly externalEffects: "stub";
  /** The snapshot event the case restores. Default: the latest fork point. */
  readonly at?: EventId;
  /** Default: cases. The case is <dir>/<name>/. */
  readonly dir?: string;
};

export type SavedCase = { readonly path: string; readonly portable: boolean };

export type SaveContext = {
  readonly artifacts: ArtifactStore;
  readonly sandbox: Sandbox | undefined;
  readonly points: readonly ForkPoint[];
};

const IMPLS: readonly Impl[] = ["threads-ts", "threads-py"];

function invalid(message: string): Result<never, LogError> {
  return err(logError("invalid_request", message));
}

function checkRequest(
  options: SaveCaseOptions,
  name: string,
  sandbox: Sandbox | undefined,
): Result<void, LogError> {
  if (options.expect.must.length === 0)
    return invalid("a case needs an assertion");
  if (!/^[a-z0-9][a-z0-9-]*$/.test(name))
    return invalid(`case name ${name} is not lowercase-kebab`);
  return sandbox !== undefined && sandbox.info.egress !== "enforced"
    ? err(
        logError(
          "egress_policy_unsupported",
          "a case needs a sandbox that enforces deny-all egress",
        ),
      )
    : ok(undefined);
}

type Replay = {
  readonly text: string;
  /** The user_input and the rest of its turn, through turn_completed. */
  readonly appended: readonly KnownEvent[];
};

/** The next input after the snapshot and the turn it started: what the case replays. */
function replayAfter(
  events: readonly KnownEvent[],
  seq: number,
): Result<Replay, LogError> {
  const start = events.findIndex((e) => e.seq > seq && e.type === "user_input");
  const input = events[start];
  if (input?.type !== "user_input")
    return invalid("no user_input after the snapshot to replay");
  if (input.data.text === undefined)
    return invalid(
      "the next input has content parts; input.text can't carry it",
    );
  const end = events.findIndex(
    (e, i) => i > start && e.type === "turn_completed",
  );
  if (end === -1) return invalid("the turn after the snapshot never completed");
  return ok({ text: input.data.text, appended: events.slice(start, end + 1) });
}

/** Every artifact ref anywhere in a value. */
function refsIn(value: unknown, into: Set<string>): void {
  if (Array.isArray(value)) for (const v of value) refsIn(v, into);
  else if (typeof value === "object" && value !== null) {
    const ref = ArtifactRef.safeParse(value);
    if (ref.success) into.add(ref.data.sha256);
    else for (const v of Object.values(value)) refsIn(v, into);
  }
}

type Files = ReadonlyMap<string, Uint8Array | object>;

function write(path: string, files: Files): void {
  mkdirSync(join(path, "artifacts"), { recursive: true });
  for (const [name, body] of files)
    writeFileSync(
      join(path, name),
      body instanceof Uint8Array ? body : `${JSON.stringify(body, null, 2)}\n`,
    );
}

/** Every file of the case directory, by its path inside it. */
function caseFiles(
  read: VerifiedLog,
  branchId: BranchId,
  name: string,
  point: ForkPoint,
  replay: Replay,
  options: SaveCaseOptions,
  context: SaveContext & { readonly now: number },
): Result<Files, LogError> {
  const files = new Map<string, Uint8Array | object>();
  const stubs = stubScript(replay.appended, context.artifacts);
  for (const impl of IMPLS) {
    const bytes = caseLog(read, point.seq, impl);
    if (!bytes.ok) return bytes;
    const verified = verifyExport(bytes.value);
    if (!verified.ok) return verified;
    files.set(`log.${impl}.jsonl`, bytes.value);
    files.set(`expected.${impl}.json`, {
      outcome: "ok",
      state: JSON.parse(JSON.stringify(reduce(verified.value, context.now))),
      appended: replay.appended.map((e) => ({ type: e.type })),
      stubs: { consumed: stubs.stubs.length, unmatched: 0 },
    });
  }
  const refs = new Set<string>();
  refsIn(
    knownEvents(read).filter((e) => e.seq <= point.seq),
    refs,
  );
  for (const sha of refs) {
    const got = context.artifacts.get(sha);
    if (!got.ok)
      return err(logError("case_missing_dependency", got.error.message));
    files.set(join("artifacts", sha), got.value);
  }
  const sandbox = sandboxScript(replay.appended);
  if (sandbox !== undefined) files.set("sandbox.json", sandbox);
  files.set("model.json", modelScript(replay.appended));
  files.set("stubs.json", stubs);
  files.set("case.json", {
    name,
    family: "log_fork_test",
    kind: "stub",
    description: `Saved from branch ${branchId} at snapshot ${point.event_id} (seq ${point.seq}): replays the next turn with every mediated operation stubbed.`,
    clock: { now: context.now },
    model_script: "model.json",
    ...(sandbox === undefined ? {} : { sandbox_script: "sandbox.json" }),
    stub_script: "stubs.json",
    input: { text: replay.text },
    expect: { must: options.expect.must, expect: options.expect.expect ?? [] },
  });
  return ok(files);
}

export function saveCase(
  log: LogStore,
  branchId: BranchId,
  name: string,
  options: SaveCaseOptions,
  context: SaveContext,
): Result<SavedCase, LogError> {
  const valid = checkRequest(options, name, context.sandbox);
  if (!valid.ok) return valid;
  const point =
    options.at === undefined
      ? context.points.at(-1)
      : context.points.find((p) => p.event_id === options.at);
  if (point === undefined)
    return err(
      logError("no_snapshot_boundary", "no eligible snapshot to restore"),
    );
  const read = log.read(branchId);
  if (!read.ok) return read;
  const replay = replayAfter(knownEvents(read.value), point.seq);
  if (!replay.ok) return replay;
  for (const m of options.expect.must)
    if (!replay.value.appended.some((e) => matches(m, e)))
      return invalid(
        `must ${m.type} matches nothing the recorded turn appended`,
      );
  const files = caseFiles(
    read.value,
    branchId,
    name,
    point,
    replay.value,
    options,
    {
      ...context,
      now: log.now(),
    },
  );
  if (!files.ok) return files;
  const path = join(options.dir ?? "cases", name);
  write(path, files.value);
  return ok({ path, portable: point.snapshot.provider === "fake" });
}
