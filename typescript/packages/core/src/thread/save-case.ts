import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { ArtifactRef, type BranchId, type EventId } from "../log";
import { knownEvents } from "../reduce";
import { err, ok, type Result } from "../result";
import type { Sandbox } from "../sandbox/protocol";
import type { ArtifactStore, LogStore } from "../store";
import { type LogError, logError } from "../verify/error";
import type { ForkPoint } from "./open";

// saveCase(): the branch export, the artifacts it references and a
// case.json with the assertion, the snapshot the case restores and its declared dependencies.

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

/** api.json CaseExpectation: `must` fails the case when unmatched; `expect` is only recorded. */
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

/** Every artifact ref anywhere in a value. */
function refsIn(value: unknown, into: Map<string, ArtifactRef>): void {
  if (Array.isArray(value)) for (const v of value) refsIn(v, into);
  else if (typeof value === "object" && value !== null) {
    const ref = ArtifactRef.safeParse(value);
    if (ref.success) into.set(ref.data.sha256, ref.data);
    else for (const v of Object.values(value)) refsIn(v, into);
  }
}

function request(
  options: SaveCaseOptions,
  name: string,
  sandbox: Sandbox | undefined,
): Result<void, LogError> {
  if (options.expect.must.length === 0)
    return err(logError("invalid_request", "a case needs an assertion"));
  if (!/^[A-Za-z0-9._-]+$/.test(name) || name.startsWith("."))
    return err(logError("invalid_request", `bad case name ${name}`));
  return sandbox !== undefined && sandbox.info.egress !== "enforced"
    ? err(
        logError(
          "egress_policy_unsupported",
          "a case needs a sandbox that enforces deny-all egress",
        ),
      )
    : ok(undefined);
}

export function saveCase(
  log: LogStore,
  branchId: BranchId,
  name: string,
  options: SaveCaseOptions,
  context: {
    readonly artifacts: ArtifactStore;
    readonly sandbox: Sandbox | undefined;
    readonly points: readonly ForkPoint[];
  },
): Result<SavedCase, LogError> {
  const valid = request(options, name, context.sandbox);
  if (!valid.ok) return valid;
  const point =
    options.at === undefined
      ? context.points.at(-1)
      : context.points.find((p) => p.event_id === options.at);
  if (point === undefined)
    return err(
      logError("no_snapshot_boundary", "no eligible snapshot to restore"),
    );
  const bytes = log.exportBranch(branchId);
  const read = log.read(branchId);
  if (!bytes.ok) return bytes;
  if (!read.ok) return read;
  const refs = new Map<string, ArtifactRef>();
  refsIn(knownEvents(read.value), refs);
  const artifacts: [string, Uint8Array][] = [];
  for (const sha of refs.keys()) {
    const got = context.artifacts.get(sha);
    if (!got.ok)
      return err(logError("case_missing_dependency", got.error.message));
    artifacts.push([sha, got.value]);
  }
  const path = join(options.dir ?? "cases", name);
  mkdirSync(join(path, "artifacts"), { recursive: true });
  writeFileSync(join(path, "log.jsonl"), bytes.value);
  for (const [sha, data] of artifacts)
    writeFileSync(join(path, "artifacts", sha), data);
  const portable = point.snapshot.provider === "fake";
  const manifest = {
    name,
    external_effects: options.externalEffects,
    expect: { must: options.expect.must, expect: options.expect.expect ?? [] },
    snapshot: { event_id: point.event_id, seq: point.seq, ...point.snapshot },
    dependencies: {
      sandbox_provider: point.snapshot.provider,
      capture_classes: [point.snapshot.capture_class],
      snapshot_expires_at: point.snapshot.expires_at,
      model: "recorded",
    },
    portable,
  };
  writeFileSync(
    join(path, "case.json"),
    `${JSON.stringify(manifest, null, 2)}\n`,
  );
  return ok({ path, portable });
}
