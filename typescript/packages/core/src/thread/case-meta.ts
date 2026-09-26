import type { CaseSimulate, SimulateBlocked } from "../evals/files";
import { sha256Hex } from "../hash";
import type { offlineReason, Turn } from "./case-turn";
import type { ForkPoint } from "./handle";
import type { CaseExpectation } from "./save-case";

// case.json, as saveCase writes it (spec/conformance/case.schema.json Case): the scripts it names,
// the turn's input and assertion, and the optional fields each lane added -- the rubric, the
// simulated user, the sandbox snapshot, why it can't rerun offline, and the prompt prefix.

export type CaseParts = {
  readonly name: string;
  readonly turn: Turn;
  readonly now: number;
  readonly expect: CaseExpectation;
  readonly rubric: readonly string[] | undefined;
  readonly simulate: CaseSimulate | undefined;
  readonly blocked: SimulateBlocked | undefined;
  readonly sandbox: object | undefined;
  readonly extensions: object | undefined;
  readonly line0: Uint8Array | undefined;
  readonly snapshot: ForkPoint | undefined;
  readonly offline: ReturnType<typeof offlineReason>;
};

export function caseMeta(p: CaseParts): object {
  const { turn } = p;
  return {
    name: p.name,
    family: "log_fork_test",
    kind: "stub",
    description: `Saved from branch ${turn.input.branch_id}: replays the turn of input ${turn.input.event_id} with every mediated operation stubbed.`,
    clock: { now: p.now },
    model_script: "model.json",
    ...(p.sandbox === undefined ? {} : { sandbox_script: "sandbox.json" }),
    stub_script: "stubs.json",
    ...(p.extensions === undefined
      ? {}
      : { extension_script: "extensions.json" }),
    input:
      turn.input.data.text === undefined ? {} : { text: turn.input.data.text },
    expect: { must: p.expect.must, expect: p.expect.expect ?? [] },
    ...(p.rubric === undefined ? {} : { rubric: p.rubric }),
    ...(p.simulate === undefined ? {} : { simulate: p.simulate }),
    ...(p.blocked === undefined ? {} : { simulate_blocked: p.blocked }),
    ...(p.snapshot === undefined
      ? {}
      : {
          snapshot: {
            event_id: p.snapshot.event_id,
            provider: p.snapshot.snapshot.provider,
          },
        }),
    ...(p.offline === undefined
      ? {}
      : { offline: { runnable: false, ...p.offline } }),
    ...(p.line0 === undefined ? {} : { line0: { sha256: sha256Hex(p.line0) } }),
  };
}
