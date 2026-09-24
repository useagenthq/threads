import { z } from "zod";
import { memoryStore } from "../agent/sqlite";
import type { KnownEvent, Policy, ToolSpec } from "../log";
import { type LoopConfig, type LoopEnd, recordedStubs, resume } from "../loop";
import { knownEvents } from "../reduce";
import { refReader } from "../render";
import { readSpec } from "../render/tool-specs";
import type { SandboxScript } from "../sandbox/script";
import type { ArtifactStore, EventDraft, Writer } from "../store";
import { verifyExport } from "../verify";
import { answers, type Counters } from "./answers";
import type { ExtensionScript, SandboxResults } from "./files";
import { recordedAuthorizer, replayedModels } from "./replayed";
import { standIns } from "./stand-in";

// The rerun (spec lane 22, B.2), promoted from the conformance runner, which now imports it:
// import the case log into a private in-memory store, take the lease (recovery runs first),
// send the recorded input, and loop against the recorded model replies, tool results, stubs,
// hook decisions and recall until the branch is idle or parked. No sandbox, no provider.

export type RerunInput = {
  readonly log: Uint8Array;
  readonly artifacts: readonly Uint8Array[];
  readonly now: number;
  /** model.json; absent: recovery only, the loop doesn't run. */
  readonly model: unknown;
  readonly sandbox: SandboxResults | SandboxScript | undefined;
  /** stubs.json: every mediated call answers from it, and an unmatched one fails closed. */
  readonly stubs: unknown;
  readonly extensions: ExtensionScript | undefined;
  /** What to send once the log is imported: the recorded user_input. */
  readonly input: EventDraft | undefined;
  /** The recorded turn, whose permission decisions stand; absent: the conformance policy. */
  readonly recorded: readonly KnownEvent[] | undefined;
};

export type Rerun =
  | {
      readonly kind: "refused";
      readonly code: string;
      readonly seq: number | undefined;
    }
  | {
      readonly kind: "ran";
      readonly end: LoopEnd;
      /** Everything this run appended, acquiring included. */
      readonly appended: readonly KnownEvent[];
      readonly scriptLeft: number;
      readonly unexpected: number;
      readonly counters: Counters;
      readonly stubs:
        | { readonly consumed: number; readonly unmatched: number }
        | undefined;
      readonly unrecordedCalls: number;
      readonly unrecordedHooks: number;
      /** Recorded results, recall and stubs no call used. */
      readonly left: number;
      /** The branch's export after the run: it must verify. */
      readonly exported: Uint8Array;
    };

const HOLDER = "eval-rerun";

/** The pinned output schema, compiled; a schema Zod can't read is taken as recorded. */
function outputOf(policy: Policy | undefined): z.ZodType | undefined {
  if (policy?.output === undefined) return undefined;
  try {
    return z.fromJSONSchema(policy.output.schema);
  } catch {
    return z.json();
  }
}

function config(
  input: RerunInput,
  writer: Writer,
  artifacts: ArtifactStore,
  clock: { now: number },
) {
  const events = () => knownEvents(writer.chain);
  const replayed = replayedModels(
    input.model ?? { responses: [] },
    events,
    input.recorded !== undefined,
  );
  const tools = answers({
    specs: pinnedSpecs(writer, artifacts),
    sandbox: input.sandbox,
    recall: input.extensions?.recall ?? [],
    artifacts,
    now: () => clock.now,
  });
  const stubs =
    input.stubs === undefined ? undefined : recordedStubs(input.stubs);
  const hooks = standIns(input.extensions);
  const output = outputOf(writer.chain.fold.policy);
  const started = events().find((e) => e.type === "thread_started");
  const loop: LoopConfig = {
    models: replayed.models,
    tools: tools.tools,
    authorize:
      input.recorded === undefined
        ? scriptedPolicy(input.sandbox)
        : recordedAuthorizer(input.recorded),
    clock: {
      now: () => clock.now,
      sleepUntil: async (t) => {
        clock.now = Math.max(clock.now, t);
      },
    },
    principal: { issuer: "threads", tenant: "evals", subject: "eval-runner" },
    skewMarginMs: 1000,
    // The thread is its own team lead; no subagents (spec/conformance/README.md, recover 5).
    agents: {
      name: started?.type === "thread_started" ? started.data.agent_name : "",
      subagent: () => undefined,
      subagents: [],
    },
    extensions: hooks.extensions,
    ...(stubs === undefined ? {} : { stub: stubs }),
    ...(output === undefined ? {} : { output }),
  };
  return { loop, replayed, tools, stubs, hooks };
}

/**
 * Every tool the thread pinned or later added, as the host binds them whatever the latest set
 * holds; a deferred tool by the full spec its spec_ref artifact holds.
 */
function pinnedSpecs(writer: Writer, artifacts: ArtifactStore): ToolSpec[] {
  const read = refReader(artifacts);
  return [...writer.chain.fold.knownTools.values()].map((spec) => {
    if (spec.spec_ref === undefined) return spec;
    const full = readSpec(read, spec.spec_ref, 0);
    if (!full.ok)
      throw new Error(`a pinned spec_ref reads back: ${full.error.message}`);
    return full.value;
  });
}

/** The corpus's policy: the decision a scripted tool names, else allow (case.schema.json). */
function scriptedPolicy(
  sandbox: RerunInput["sandbox"],
): LoopConfig["authorize"] {
  const tools =
    sandbox === undefined || "results" in sandbox ? {} : (sandbox.tools ?? {});
  return (call) => {
    const decision = tools[call.data.name]?.decision ?? "allow";
    return { decision, source: "policy", rule_id: `conformance_${decision}` };
  };
}

export async function rerun(input: RerunInput): Promise<Rerun> {
  const clock = { now: input.now };
  const store = await memoryStore(() => clock.now);
  try {
    for (const artifact of input.artifacts) store.artifacts.put(artifact);
    const imported = store.log.importLog(input.log);
    if (!imported.ok) return refused(imported.error);
    const leaf = imported.value.segments.at(-1)?.header;
    if (leaf === undefined) throw new Error("a verified log has a header");
    const acquired = store.log.acquire(leaf.branch_id, HOLDER);
    if (!acquired.ok) return refused(acquired.error);
    const writer = acquired.value;
    const before = imported.value.fold.seq;
    const run = config(input, writer, store.artifacts, clock);
    const end = await resume(writer, store.artifacts, run.loop, {
      loop: input.model !== undefined,
      ...(input.input === undefined ? {} : { input: input.input }),
    });
    const exported = store.log.exportBranch(leaf.branch_id);
    if (!exported.ok || !verifyExport(exported.value).ok)
      throw new Error("a rerun's branch exports and verifies");
    return {
      kind: "ran",
      end,
      appended: knownEvents(writer.chain).filter((e) => e.seq > before),
      scriptLeft: run.replayed.script.remaining(),
      unexpected: run.replayed.script.unexpected(),
      counters: run.tools.counters(),
      stubs:
        run.stubs === undefined
          ? undefined
          : {
              consumed: run.stubs.consumed(),
              unmatched: run.stubs.unmatched(),
            },
      unrecordedCalls: run.tools.unrecorded(),
      unrecordedHooks: run.hooks.unrecorded(),
      left: run.tools.left() + (run.stubs?.left() ?? 0),
      exported: exported.value,
    };
  } finally {
    store.close();
  }
}

function refused(error: {
  readonly code: string;
  readonly seq?: number;
}): Rerun {
  return { kind: "refused", code: error.code, seq: error.seq };
}
