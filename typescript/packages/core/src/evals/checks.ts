import type { DryPin } from "../agent/registry";
import { canonicalize, type KnownEvent, type ToolSpec } from "../log";
import { knownEvents } from "../reduce";
import { refReader, verifyRequests } from "../render";
import { memoryArtifacts } from "../store";
import { verifyExport } from "../verify";
import { type CaseDir, inputOf } from "./case-dir";
import {
  firstLine,
  firstMismatch,
  type Matcher,
  type Mismatch,
  matches,
} from "./compare";
import { drift } from "./drift";
import { offlineBlock } from "./offline";
import { rerun } from "./rerun";
import type { Checks, DriftCheck } from "./schema";

// The free checks of one case (spec lane 22, B.1-B.3), cheapest first: replay the recorded
// requests with the code running now, rerun the recorded turn offline, and compare the recorded
// config with the agent as it is pinned now. Each returns a value; only bugs throw.

type Replay = NonNullable<Checks["replay"]>;
type Rerun = NonNullable<Checks["rerun"]>;

/** What the case log holds before the turn: its events and the tools in effect. */
export type CaseLog = {
  readonly events: readonly KnownEvent[];
  readonly tools: readonly ToolSpec[];
};

/** B.1: the verifier Thread.replay() runs, over the case log and its artifacts. */
export function replayCheck(c: CaseDir): {
  readonly check: Replay;
  readonly log?: CaseLog;
} {
  const verified = verifyExport(c.log);
  if (!verified.ok) {
    const { code, seq } = verified.error;
    return {
      check: { ok: false, code, ...(seq === undefined ? {} : { seq }) },
    };
  }
  const artifacts = memoryArtifacts();
  for (const a of c.artifacts) artifacts.put(a);
  const events = knownEvents(verified.value);
  const checked = verifyRequests(events, refReader(artifacts));
  if (!checked.ok) {
    const { code, seq } = checked.error;
    return {
      check: { ok: false, code, ...(seq === undefined ? {} : { seq }) },
    };
  }
  return {
    check: { ok: true },
    log: { events, tools: verified.value.fold.tools },
  };
}

/** Why the case can't rerun offline, as the report states it. */
export function skipReason(c: CaseDir, log: CaseLog): string | undefined {
  const block = offlineBlock(c, log.tools);
  if (block === undefined) return undefined;
  const types = c.meta.offline?.types ?? [];
  return `offline_not_runnable:${block}${types.length === 0 ? "" : ` (${types.join(", ")})`}`;
}

const V1_HINT = "re-save this case (sandbox.json v1 keeps previews only)";

/** An older case's `appended` matchers: exact count and order, each matching its event. */
function matcherMismatch(
  want: readonly Matcher[],
  got: readonly KnownEvent[],
): Mismatch | undefined {
  const length = Math.max(want.length, got.length);
  for (let index = 0; index < length; index += 1) {
    const m = want[index];
    const e = got[index];
    if (m === undefined || e === undefined || !matches(m, e))
      return { index, want: m?.type ?? null, got: e?.type ?? null };
  }
  return undefined;
}

function mismatchOf(c: CaseDir, got: readonly KnownEvent[]): Rerun["mismatch"] {
  const found =
    c.recorded !== undefined
      ? firstMismatch(c.recorded, got)
      : c.matchers === undefined
        ? undefined
        : matcherMismatch(c.matchers, got);
  if (found === undefined) return undefined;
  const v1 = c.sandbox !== undefined && !("results" in c.sandbox);
  return v1 && found.want === "tool_result"
    ? { ...found, hint: V1_HINT }
    : found;
}

/** B.2: the promoted rerun, and how it compares with the recording. */
export async function rerunCheck(
  c: CaseDir,
): Promise<{ readonly check: Rerun } | { readonly error: string }> {
  const out = await rerun({
    log: c.log,
    artifacts: c.artifacts,
    now: c.meta.clock.now,
    model: c.model,
    sandbox: c.sandbox,
    stubs: c.stubs,
    extensions: c.extensions,
    input: inputOf(c),
    recorded: c.recorded,
  });
  if (out.kind === "refused") return { error: `rerun: ${out.code}` };
  const mismatch = mismatchOf(c, out.appended);
  const unmatched = c.meta.expect.must.filter(
    (m) => !out.appended.some((e) => matches(m, e)),
  );
  const check: Rerun = {
    ok: false,
    unmatched,
    script_left: out.scriptLeft,
    stubs_unmatched: out.stubs?.unmatched ?? 0,
    unrecorded_calls: out.unrecordedCalls,
    unrecorded_hooks: out.unrecordedHooks,
    ...(mismatch === undefined ? {} : { mismatch }),
  };
  const clean =
    mismatch === undefined &&
    unmatched.length === 0 &&
    out.scriptLeft + out.unexpected + out.left === 0 &&
    check.stubs_unmatched + check.unrecorded_calls + check.unrecorded_hooks ===
      0;
  return { check: { ...check, ok: clean } };
}

function described(m: Matcher): string {
  if (m.data === undefined) return m.type;
  const data = canonicalize(JSON.parse(JSON.stringify(m.data)));
  return data.ok ? `${m.type}${data.value}` : m.type;
}

/** Why a rerun failed, in one line: the first of its problems in a fixed order. */
export function rerunReason(r: Rerun): string {
  const first = r.unmatched[0];
  const m = r.mismatch;
  if (r.unrecorded_hooks > 0) return "rerun: unrecorded_hook";
  if (r.unrecorded_calls > 0) return "rerun: unrecorded_call";
  if (r.stubs_unmatched > 0) return "rerun: unmatched_external_op";
  if (m !== undefined)
    return `rerun: event ${m.index} is ${m.got ?? "missing"}, recorded ${m.want ?? "nothing"}${m.hint === undefined ? "" : `; ${m.hint}`}`;
  if (first !== undefined) return `rerun: unmatched ${described(first)}`;
  if (r.script_left > 0)
    return `rerun: ${r.script_left} recorded model replies left`;
  return "rerun: recorded results left over";
}

/** B.3: the recorded config against the agents given, by their dry pins. */
export function driftCheck(
  c: CaseDir,
  log: CaseLog,
  pins: readonly DryPin[],
): DriftCheck | undefined {
  const started = log.events.find((e) => e.type === "thread_started");
  if (started?.type !== "thread_started") return undefined;
  return drift(
    { started: started.data, line0: c.line0 ?? lastLine0(c, log) },
    pins,
  );
}

/** A case saved before line0.json: line 0 of its log's last recorded request, if any. */
function lastLine0(c: CaseDir, log: CaseLog): Uint8Array | undefined {
  const request = log.events.findLast((e) => e.type === "model_request");
  if (request?.type !== "model_request") return undefined;
  const artifacts = memoryArtifacts();
  for (const a of c.artifacts) artifacts.put(a);
  const bytes = artifacts.get(request.data.request_ref.sha256);
  return bytes.ok ? firstLine(bytes.value) : undefined;
}

/** A drift result as its one-line report reason. */
export function driftReason(d: DriftCheck): string {
  if (d.agents !== undefined) return "agent_not_found";
  const added = (d.tools?.added ?? []).map((n) => `+${n}`);
  const removed = (d.tools?.removed ?? []).map((n) => `-${n}`);
  const changed = (d.tools?.changed ?? []).map((n) => `~${n}`);
  const names = [...added, ...removed, ...changed];
  const kinds = d.kinds
    .map((k) =>
      k === "tools" && names.length > 0 ? `tools (${names.join(", ")})` : k,
    )
    .join(", ");
  return `drift: ${kinds}`;
}
