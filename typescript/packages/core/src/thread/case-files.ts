import { sha256Hex } from "../hash";
import { canonicalize, type KnownEvent } from "../log";
import type { ArtifactStore } from "../store";
import type { EventMatcher } from "./save-case";

// The scripts a saved case replays its next turn against (spec/conformance/case.schema.json
// ModelScript, StubScript, SandboxScript), read off the turn the branch recorded after the
// snapshot. Built from typed values, so they are valid by construction.

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

export type SandboxScriptFile = {
  readonly tools: Readonly<
    Record<string, { readonly output: string; readonly is_error: boolean }>
  >;
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
function outputOf(
  turn: readonly KnownEvent[],
  callId: string,
  artifacts: ArtifactStore,
): string {
  const commit = turn.find(
    (e): e is Of<"effect_commit"> =>
      e.type === "effect_commit" && e.data.call_id === callId,
  );
  const bytes =
    commit === undefined
      ? undefined
      : artifacts.get(commit.data.result_ref.sha256);
  if (bytes?.ok === true) return new TextDecoder().decode(bytes.value);
  return resultOf(turn, callId)?.data.preview ?? "";
}

const begun = (turn: readonly KnownEvent[], callId: string): boolean =>
  turn.some((e) => e.type === "effect_begin" && e.data.call_id === callId);

/** Every mediated call (one with effect events) as a stub, by (tool, args_hash, occurrence). */
export function stubScript(
  turn: readonly KnownEvent[],
  artifacts: ArtifactStore,
): StubScriptFile {
  const seen = new Map<string, number>();
  const stubs = turn.flatMap((e) => {
    if (e.type !== "tool_call" || !begun(turn, e.data.call_id)) return [];
    const args = canonicalize(e.data.input);
    if (!args.ok) throw new Error("tool_call input is canonical JSON");
    const argsHash = sha256Hex(args.value);
    const key = `${e.data.name}\n${argsHash}`;
    const occurrence = seen.get(key) ?? 0;
    seen.set(key, occurrence + 1);
    return [
      {
        tool: e.data.name,
        args_hash: argsHash,
        occurrence,
        output: outputOf(turn, e.data.call_id, artifacts),
        is_error: resultOf(turn, e.data.call_id)?.data.is_error ?? false,
      },
    ];
  });
  return { stubs };
}

/**
 * Read-only calls run in the sandbox, not the gateway: their recorded output as the fake
 * sandbox's scripted tools. One output per tool name (the first call's).
 */
export function sandboxScript(
  turn: readonly KnownEvent[],
): SandboxScriptFile | undefined {
  const tools: Record<string, { output: string; is_error: boolean }> = {};
  for (const e of turn) {
    if (e.type !== "tool_call" || begun(turn, e.data.call_id)) continue;
    const result = resultOf(turn, e.data.call_id);
    tools[e.data.name] ??= {
      output: result?.data.preview ?? "",
      is_error: result?.data.is_error ?? false,
    };
  }
  return Object.keys(tools).length === 0 ? undefined : { tools };
}

/** An EventMatcher against one event: listed envelope keys exactly, data as a deep subset. */
export function matches(m: EventMatcher, e: KnownEvent): boolean {
  const envelope: readonly [unknown, unknown][] = [
    [m.type, e.type],
    [m.seq, e.seq],
    [m.actor_kind, e.actor.kind],
    [m.epoch, e.epoch],
    [m.branch_id, e.branch_id],
    [m.critical, e.critical],
  ];
  return (
    envelope.every(([want, got]) => want === undefined || want === got) &&
    (m.data === undefined || subset(m.data, e.data))
  );
}

function subset(want: unknown, got: unknown): boolean {
  const isObject = (v: unknown): v is object =>
    typeof v === "object" && v !== null && !Array.isArray(v);
  if (!isObject(want) || !isObject(got))
    return JSON.stringify(want) === JSON.stringify(got);
  return Object.entries(want).every(([k, v]) => subset(v, Reflect.get(got, k)));
}
