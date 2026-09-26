import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { firstLine, matches } from "../evals/compare";
import type { OfflineReason } from "../evals/files";
import { Rubric } from "../evals/schema";
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
import { modelScript, sandboxResults, stubsOf } from "./case-files";
import { extensionScript } from "./case-hooks";
import { caseLog, type Impl } from "./case-log";
import { caseMeta } from "./case-meta";
import { prefixStubs, type SimulateUser, simulateField } from "./case-simulate";
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
  /**
   * Turn the case into a multi-turn live eval: the saved turn's text is the first user message,
   * and this plays the user after it (spec lane 32). Offline nothing changes.
   */
  readonly simulate?: SimulateUser;
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

/** A recorded output the case can't carry: as unreplayable as a missing chain artifact. */
function missing(error: LogError): Result<never, LogError> {
  return err(logError("case_missing_dependency", error.message, error.seq));
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
  if (options.simulate !== undefined) {
    const checked = simulateField(options.simulate);
    if (!checked.ok) return checked;
  }
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
export function refsIn(value: unknown, into: Set<string>): void {
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
async function copy(
  files: Files,
  refs: ReadonlySet<string>,
  artifacts: ArtifactStore,
): Promise<readonly string[]> {
  const gone: string[] = [];
  for (const sha of refs) {
    const got = await artifacts.get(sha);
    if (got.ok) files.set(join("artifacts", sha), got.value);
    else gone.push(sha);
  }
  return gone;
}

/** Line 0 of the turn's first request artifact: what drift compares the agent against. */
async function line0(
  turn: Turn,
  artifacts: ArtifactStore,
): Promise<Uint8Array | undefined> {
  const request = turn.events.find((e) => e.type === "model_request");
  if (request?.type !== "model_request") return undefined;
  const bytes = await artifacts.get(request.data.request_ref.sha256);
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
async function logFiles(
  saving: Saving,
  files: Files,
  stubs: number,
): Promise<Result<void, LogError>> {
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
  const gone = await copy(files, refs, context.artifacts);
  return gone[0] === undefined
    ? ok(undefined)
    : err(logError("case_missing_dependency", `artifact ${gone[0]} is gone`));
}

/** Every file of the case directory, by its path inside it, and why it can't rerun offline. */
async function caseFiles(saving: Saving): Promise<
  Result<
    {
      readonly files: Files;
      readonly offline: ReturnType<typeof offlineReason>;
    },
    LogError
  >
> {
  const { read, turn, name, options, context } = saving;
  const files: Files = new Map();
  const all = knownEvents(read);
  const before = all.filter((e) => e.seq <= turn.restoreSeq);
  const built = await stubsOf(turn.events, context.artifacts);
  // A mediated call whose committed output is gone makes the case unreplayable, the same way a
  // missing chain artifact does.
  if (!built.ok) return missing(built.error);
  const turnStubs = built.value;
  const earlier =
    options.simulate === undefined
      ? undefined
      : await prefixStubs(before, turnStubs, all, context.artifacts);
  if (earlier !== undefined && !earlier.ok) return missing(earlier.error);
  const stubs = { stubs: earlier?.value.stubs ?? turnStubs };
  const blocked = earlier?.value.blocked;
  const logged = await logFiles(saving, files, turnStubs.length);
  if (!logged.ok) return logged;
  const turnRefs = new Set<string>();
  refsIn(
    turn.events.filter((e) => e.type !== "model_request"),
    turnRefs,
  );
  const later = all.filter((e) => e.seq > turn.restoreSeq);
  const checked =
    options.simulate === undefined
      ? undefined
      : simulateField(options.simulate);
  const simulate = checked?.ok === true ? checked.value : undefined;
  const offline =
    (await copy(files, turnRefs, context.artifacts)).length > 0
      ? { reason: "artifact_missing" as const }
      : offlineReason(turn, later);
  const sandbox = sandboxResults(turn.events);
  const extensions = extensionScript(turn.events);
  const prefix = await line0(turn, context.artifacts);
  if (sandbox !== undefined) files.set("sandbox.json", sandbox);
  if (extensions !== undefined) files.set("extensions.json", extensions);
  if (prefix !== undefined) files.set("line0.json", prefix);
  files.set("model.json", modelScript(turn.events));
  files.set("stubs.json", stubs);
  files.set(
    "case.json",
    caseMeta({
      name,
      turn,
      now: context.now,
      expect: options.expect,
      rubric: options.rubric,
      simulate,
      blocked,
      sandbox,
      extensions,
      line0: prefix,
      snapshot: context.points.find((p) => p.seq === turn.restoreSeq),
      offline,
    }),
  );
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

export async function saveCase(
  log: LogStore,
  branchId: BranchId,
  name: string,
  options: SaveCaseOptions,
  context: SaveContext,
): Promise<Result<SavedCase, LogError>> {
  const valid = checkRequest(options, name, context.sandbox);
  if (!valid.ok) return valid;
  const read = await log.read(branchId);
  if (!read.ok) return read;
  const turn = findTurn(read.value, options.at);
  if (!turn.ok) return turn;
  const met = checkMust(options.expect.must, turn.value.events);
  if (!met.ok) return met;
  const built = await caseFiles({
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
