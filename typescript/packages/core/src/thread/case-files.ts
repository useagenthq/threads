import type { SandboxResult, SandboxResults } from "../evals/files";
import { sha256Hex } from "../hash";
import { canonicalize, type KnownEvent } from "../log";
import type { ArtifactStore } from "../store";
import { isLoopTool } from "../tools/loop-tools";

// The scripts a saved case replays its turn against (spec/conformance/case.schema.json
// ModelScript, StubScript, SandboxScript v2), read off the recorded turn. Built from typed
// values, so they are valid by construction.

type Of<T extends KnownEvent["type"]> = Extract<KnownEvent, { type: T }>;

export type ModelScriptFile = {
  readonly responses: readonly Pick<
    Of<"model_response">["data"],
    "content" | "stop_reason" | "usage"
  >[];
};

export type StubScriptFile = {
  readonly stubs: readonly {
    readonly tool: string;
    readonly args_hash: string;
    readonly occurrence: number;
    readonly output: string;
    readonly is_error: boolean;
  }[];
};

/** The recorded model responses, in order: the case's model.json. */
export function modelScript(turn: readonly KnownEvent[]): ModelScriptFile {
  return {
    responses: turn.flatMap((e) =>
      e.type === "model_response"
        ? [
            {
              content: e.data.content,
              stop_reason: e.data.stop_reason,
              usage: e.data.usage,
            },
          ]
        : [],
    ),
  };
}

function resultOf(
  turn: readonly KnownEvent[],
  callId: string,
): Of<"tool_result"> | undefined {
  return turn.find(
    (e): e is Of<"tool_result"> =>
      e.type === "tool_result" && e.data.call_id === callId,
  );
}

/** A mediated call's full recorded output: the committed artifact, else the result preview. */
async function outputOf(
  turn: readonly KnownEvent[],
  callId: string,
  artifacts: ArtifactStore,
): Promise<string> {
  const commit = turn.find(
    (e): e is Of<"effect_commit"> =>
      e.type === "effect_commit" && e.data.call_id === callId,
  );
  const bytes =
    commit === undefined
      ? undefined
      : await artifacts.get(commit.data.result_ref.sha256);
  if (bytes?.ok === true) return new TextDecoder().decode(bytes.value);
  return resultOf(turn, callId)?.data.preview ?? "";
}

const begun = (turn: readonly KnownEvent[], callId: string): boolean =>
  turn.some((e) => e.type === "effect_begin" && e.data.call_id === callId);

/** sha256 of a call's RFC 8785 input: how stubs and sandbox results are keyed. */
export function argsHash(input: Of<"tool_call">["data"]["input"]): string {
  const args = canonicalize(input);
  if (!args.ok) throw new Error("tool_call input is canonical JSON");
  return sha256Hex(args.value);
}

/** Every mediated call (one with effect events) as a stub, by (tool, args_hash, occurrence). */
export async function stubScript(
  turn: readonly KnownEvent[],
  artifacts: ArtifactStore,
): Promise<StubScriptFile> {
  const seen = new Map<string, number>();
  const stubs: StubScriptFile["stubs"][number][] = [];
  for (const e of turn) {
    if (e.type !== "tool_call" || !begun(turn, e.data.call_id)) continue;
    const hash = argsHash(e.data.input);
    const key = `${e.data.name}\n${hash}`;
    const occurrence = seen.get(key) ?? 0;
    seen.set(key, occurrence + 1);
    stubs.push({
      tool: e.data.name,
      args_hash: hash,
      occurrence,
      output: await outputOf(turn, e.data.call_id, artifacts),
      is_error: resultOf(turn, e.data.call_id)?.data.is_error ?? false,
    });
  }
  return { stubs };
}

/**
 * sandbox.json v2: every executed read-only call's recorded result, keyed like stubs.json but
 * counting occurrences from 1, with its content parts and spill ref so it replays in full.
 * Framework tools run from the log in the rerun too, so they are never recorded.
 */
export function sandboxResults(
  turn: readonly KnownEvent[],
): SandboxResults | undefined {
  const seen = new Map<string, number>();
  const results = turn.flatMap((e): SandboxResult[] => {
    if (e.type !== "tool_call" || isLoopTool(e.data.name)) return [];
    const result = resultOf(turn, e.data.call_id);
    if (begun(turn, e.data.call_id) || result?.data.origin !== "executed")
      return [];
    const hash = argsHash(e.data.input);
    const key = `${e.data.name}\n${hash}`;
    const occurrence = (seen.get(key) ?? 0) + 1;
    seen.set(key, occurrence);
    const { is_error, preview, content, ref } = result.data;
    return [
      {
        tool: e.data.name,
        args_hash: hash,
        occurrence,
        is_error,
        preview,
        ...(content === undefined ? {} : { content }),
        ...(ref === undefined ? {} : { ref }),
      },
    ];
  });
  return results.length === 0 ? undefined : { version: 2, results };
}
