import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { firstLine, matches } from "../evals/compare";
import type { OfflineReason } from "../evals/files";
import { Rubric } from "../evals/schema";
import { sha256Hex } from "../hash";
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
import { modelScript, sandboxResults, stubScript } from "./case-files";
import { extensionScript } from "./case-hooks";
import { caseLog, type Impl } from "./case-log";
import { findTurn, offlineReason, type Turn } from "./case-turn";
import type { ForkPoint } from "./handle";

// saveCase() (spec/api.json): any completed turn as a stub-kind case in the spec/conformance
// layout. The log is the branch through the event before the turn's run; the turn itself
// becomes input.text, model.json, stubs.json (mediated calls), sandbox.json (read-only results),
// extensions.json (hook and recall outcomes), line0.json and `appended`. No sandbox snapshot is
// needed: the offline rerun replays recorded results only.

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
  /**
   * The user_input of the turn to save; default the last completed turn. A snapshot event id
   * means the turn after it.
   */
  readonly at?: EventId;
  /**
   * Criteria a live eval's judge checks the current agent's work against; each passes or
   * fails, and the case passes only when all do.
   */
  readonly rubric?: readonly string[];
  /** Default: cases. The case is <dir>/<name>/. */
  readonly dir?: string;
};

export type SavedCase = {
  readonly path: string;
  /** True when the case reruns offline from its directory alone. */
  readonly portable: boolean;
  /** Why it can't, when it can't. */
  readonly reason?: OfflineReason;
};

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
  if (options.rubric !== undefined && !Rubric.safeParse(options.rubric).success)
    return invalid(
      "rubric: 1 to 20 criteria, each 1 to 500 characters; pass rubric: undefined for none",
    );
  return sandbox !== undefined && sandbox.info.egress !== "enforced"
    ? err(
        logError(
          "egress_policy_unsupported",
          "a case needs a sandbox that enforces deny-all egress",
        ),
      )
    : ok(undefined);
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

type Files = Map<string, Uint8Array | object>;

function write(path: string, files: Files): void {
  mkdirSync(join(path, "artifacts"), { recursive: true });
  for (const [name, body] of files)
    writeFileSync(
      join(path, name),
      body instanceof Uint8Array ? body : `${JSON.stringify(body, null, 2)}\n`,
    );
}

/** Copies each ref's bytes into artifacts/; the shas that are gone from the store. */
function copy(
  files: Files,
  refs: ReadonlySet<string>,
  artifacts: ArtifactStore,
): readonly string[] {
  const gone: string[] = [];
  for (const sha of refs) {
    const got = artifacts.get(sha);
    if (got.ok) files.set(join("artifacts", sha), got.value);
    else gone.push(sha);
  }
  return gone;
}

/** Line 0 of the turn's first request artifact: what drift compares the agent against. */
function line0(turn: Turn, artifacts: ArtifactStore): Uint8Array | undefined {
  const request = turn.events.find((e) => e.type === "model_request");
  if (request?.type !== "model_request") return undefined;
  const bytes = artifacts.get(request.data.request_ref.sha256);
  return bytes.ok ? firstLine(bytes.value) : undefined;
}

type Saving = {
  readonly read: VerifiedLog;
  readonly turn: Turn;
  readonly name: string;
  readonly options: SaveCaseOptions;
  readonly context: SaveContext & { readonly now: number };
};

/** The log, expected and artifact files of the case, per implementation. */
function logFiles(
  saving: Saving,
  files: Files,
  stubs: number,
): Result<void, LogError> {
  const { read, turn, context } = saving;
  for (const impl of IMPLS) {
    const bytes = caseLog(read, turn.restoreSeq, impl);
    if (!bytes.ok) return bytes;
    const verified = verifyExport(bytes.value);
    if (!verified.ok) return verified;
    files.set(`log.${impl}.jsonl`, bytes.value);
    files.set(`expected.${impl}.json`, {
      outcome: "ok",
      state: JSON.parse(JSON.stringify(reduce(verified.value, context.now))),
      appended: turn.events,
      stubs: { consumed: stubs, unmatched: 0 },
    });
  }
  const refs = new Set<string>();
  refsIn(
    knownEvents(read).filter((e) => e.seq <= turn.restoreSeq),
    refs,
  );
  const gone = copy(files, refs, context.artifacts);
  return gone[0] === undefined
    ? ok(undefined)
    : err(logError("case_missing_dependency", `artifact ${gone[0]} is gone`));
}

/** Every file of the case directory, by its path inside it, and why it can't rerun offline. */
function caseFiles(
  saving: Saving,
): Result<
  { readonly files: Files; readonly offline: ReturnType<typeof offlineReason> },
  LogError
> {
  const { read, turn, name, options, context } = saving;
  const files: Files = new Map();
  const stubs = stubScript(turn.events, context.artifacts);
  const logged = logFiles(saving, files, stubs.stubs.length);
  if (!logged.ok) return logged;
  const turnRefs = new Set<string>();
  refsIn(
    turn.events.filter((e) => e.type !== "model_request"),
    turnRefs,
  );
  const later = knownEvents(read).filter((e) => e.seq > turn.restoreSeq);
  const offline =
    copy(files, turnRefs, context.artifacts).length > 0
      ? { reason: "artifact_missing" as const }
      : offlineReason(turn, later);
  const sandbox = sandboxResults(turn.events);
  const extensions = extensionScript(turn.events);
  const prefix = line0(turn, context.artifacts);
  if (sandbox !== undefined) files.set("sandbox.json", sandbox);
  if (extensions !== undefined) files.set("extensions.json", extensions);
  if (prefix !== undefined) files.set("line0.json", prefix);
  files.set("model.json", modelScript(turn.events));
  files.set("stubs.json", stubs);
  const snapshot = context.points.find((p) => p.seq === turn.restoreSeq);
  files.set("case.json", {
    name,
    family: "log_fork_test",
    kind: "stub",
    description: `Saved from branch ${turn.input.branch_id}: replays the turn of input ${turn.input.event_id} with every mediated operation stubbed.`,
    clock: { now: context.now },
    model_script: "model.json",
    ...(sandbox === undefined ? {} : { sandbox_script: "sandbox.json" }),
    stub_script: "stubs.json",
    ...(extensions === undefined
      ? {}
      : { extension_script: "extensions.json" }),
    input:
      turn.input.data.text === undefined ? {} : { text: turn.input.data.text },
    expect: { must: options.expect.must, expect: options.expect.expect ?? [] },
    ...(options.rubric === undefined ? {} : { rubric: options.rubric }),
    ...(snapshot === undefined
      ? {}
      : {
          snapshot: {
            event_id: snapshot.event_id,
            provider: snapshot.snapshot.provider,
          },
        }),
    ...(offline === undefined
      ? {}
      : { offline: { runnable: false, ...offline } }),
    ...(prefix === undefined ? {} : { line0: { sha256: sha256Hex(prefix) } }),
  });
  return ok({ files, offline });
}

/** Each must matcher is met by something the recorded turn appended. */
function checkMust(
  must: readonly EventMatcher[],
  turn: readonly KnownEvent[],
): Result<void, LogError> {
  const missed = must.find((m) => !turn.some((e) => matches(m, e)));
  return missed === undefined
    ? ok(undefined)
    : invalid(`must ${missed.type} matches nothing the recorded turn appended`);
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
  const read = log.read(branchId);
  if (!read.ok) return read;
  const turn = findTurn(read.value, options.at);
  if (!turn.ok) return turn;
  const met = checkMust(options.expect.must, turn.value.events);
  if (!met.ok) return met;
  const built = caseFiles({
    read: read.value,
    turn: turn.value,
    name,
    options,
    context: { ...context, now: log.now() },
  });
  if (!built.ok) return built;
  const path = join(options.dir ?? "cases", name);
  write(path, built.value.files);
  const { offline } = built.value;
  return ok(
    offline === undefined
      ? { path, portable: true }
      : { path, portable: false, reason: offline.reason },
  );
}
