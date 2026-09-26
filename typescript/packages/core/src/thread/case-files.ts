import type { SandboxResult, SandboxResults } from "../evals/files";
import { sha256Hex } from "../hash";
import { canonicalize, type KnownEvent } from "../log";
import { err, ok, type Result } from "../result";
import type { ArtifactStore } from "../store";
import { isLoopTool } from "../tools/loop-tools";
import { type LogError, logError } from "../verify/error";

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

export type StubEntry = {
  readonly tool: string;
  readonly args_hash: string;
  readonly occurrence: number;
  readonly output: string;
  readonly is_error: boolean;
  /** A simulated case's prefix effect: offline reruns skip it (spec lane 32, A.3). */
  readonly scope?: "prefix";
};

export type StubScriptFile = { readonly stubs: readonly StubEntry[] };

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

/**
 * A mediated call's complete recorded output. A committed one is read from its artifact and
 * verified (sha256 by the store, length here): a missing or corrupt one is an error naming the
 * call, never the truncated preview. A call with no `effect_commit` committed no output at all
 * (nothing was sent, or it failed), so its result preview is the whole recorded output.
 */
async function outputOf(
  turn: readonly KnownEvent[],
  callId: string,
  artifacts: ArtifactStore,
): Promise<Result<string, LogError>> {
  const commit = turn.find(
    (e): e is Of<"effect_commit"> =>
      e.type === "effect_commit" && e.data.call_id === callId,
  );
  if (commit === undefined)
    return ok(resultOf(turn, callId)?.data.preview ?? "");
  const ref = commit.data.result_ref;
  const bytes = await artifacts.get(ref.sha256);
  if (!bytes.ok)
    return err(logError(bytes.error.code, `${callId}: ${bytes.error.message}`));
  if (bytes.value.length !== ref.bytes)
    return err(
      logError(
        "artifact_corrupt",
        `${callId}: artifact ${ref.sha256} is ${bytes.value.length} bytes, not ${ref.bytes}`,
      ),
    );
  return ok(new TextDecoder().decode(bytes.value));
}

const begun = (turn: readonly KnownEvent[], callId: string): boolean =>
  turn.some((e) => e.type === "effect_begin" && e.data.call_id === callId);

/** sha256 of a call's RFC 8785 input: how stubs and sandbox results are keyed. */
export function argsHash(input: Of<"tool_call">["data"]["input"]): string {
  const args = canonicalize(input);
  if (!args.ok) throw new Error("tool_call input is canonical JSON");
  return sha256Hex(args.value);
}

/**
 * Every mediated call (one with effect events) of a slice of the log, by (tool, args_hash), each
 * holding its complete verified output. Occurrences count within the slice; a caller that joins
 * two slices renumbers them (spec lane 32, A.3).
 */
export async function stubsOf(
  events: readonly KnownEvent[],
  artifacts: ArtifactStore,
): Promise<Result<readonly StubEntry[], LogError>> {
  const stubs: StubEntry[] = [];
  for (const e of events) {
    if (e.type !== "tool_call" || !begun(events, e.data.call_id)) continue;
    const output = await outputOf(events, e.data.call_id, artifacts);
    if (!output.ok) return output;
    stubs.push({
      tool: e.data.name,
      args_hash: argsHash(e.data.input),
      occurrence: 0,
      output: output.value,
      is_error: resultOf(events, e.data.call_id)?.data.is_error ?? false,
    });
  }
  return ok(numbered(stubs));
}

/** occurrence counts earlier entries with the same (tool, args_hash), in log order. */
export function numbered(stubs: readonly StubEntry[]): readonly StubEntry[] {
  const seen = new Map<string, number>();
  return stubs.map((s) => {
    const key = `${s.tool}\n${s.args_hash}`;
    const occurrence = seen.get(key) ?? 0;
    seen.set(key, occurrence + 1);
    return { ...s, occurrence };
  });
}

/**
 * The turn's stubs as one script. A stub fork freezes this as an artifact and a saved case writes
 * it as stubs.json, so both carry the same guarantee.
 */
export async function stubScript(
  turn: readonly KnownEvent[],
  artifacts: ArtifactStore,
): Promise<Result<StubScriptFile, LogError>> {
  const built = await stubsOf(turn, artifacts);
  return built.ok ? ok({ stubs: built.value }) : built;
}

/** The script's artifact bytes: RFC 8785 canonical JSON, the same bytes in both languages. */
export function encodeStubScript(script: StubScriptFile): Uint8Array {
  const text = canonicalize({
    stubs: script.stubs.map((stub) => ({ ...stub })),
  });
  if (!text.ok) throw new Error("a stub script is JSON");
  return new TextEncoder().encode(text.value);
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
