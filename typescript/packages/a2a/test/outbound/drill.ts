import {
  canonicalize,
  JsonValue,
  type KnownEvent,
  sha256Hex,
  type ToolSpec,
} from "@threads/core/adapter";
import type { EventDraft, Writer } from "../../../core/src/store";
import { type Harness, harness } from "../../../core/test/loop/harness";
import { ROOT, THREAD, unwrap } from "../../../core/test/store/helpers";
import { type RemoteOptions, remote } from "../../src";
import { CARD_URL, type Partner } from "./partner";

// The crash-drill rig: a branch whose only tools are a real remote send and its status read, a
// scripted model that calls the send once, and a way to take the lease away at a chosen point.
// Every assertion is made against the stored events, never against a return value, because what
// the next process does is decided by the log alone.

export const TOOL = "refund_desk";

export type Drill = Harness & {
  readonly specs: readonly ToolSpec[];
  /**
   * A fresh writer, as a restarted process would take: it settles any takeover still in flight,
   * then moves the clock past the dead owner's lease, which is all a restart waits for.
   */
  readonly restart: (afterMs?: number) => Promise<Writer>;
  /** Takeovers a kill started. `onEvent` cannot be awaited, so a restart settles them. */
  readonly kills: Promise<unknown>[];
};

/** One call of the send tool, with whatever arguments the drill wants the model to have chosen. */
export function calls(input: Record<string, unknown>): unknown {
  return {
    content: [{ type: "tool_use", call_id: "call_1", name: TOOL, input }],
    stop_reason: "tool_use",
    usage: { input_tokens: 10, output_tokens: 2 },
  };
}

const CALL = calls({ message: "did refund 42 go through?" });

const DONE = {
  content: [{ type: "text", text: "Told the desk." }],
  stop_reason: "end_turn",
  usage: { input_tokens: 10, output_tokens: 2 },
};

/**
 * A branch pinned with the remote's two tools and a model that calls the send once. `timeoutMs` is
 * tiny so a follow never waits on real time: the drills are about the first answer, not the wait.
 */
export async function drill(
  p: Partner,
  options: {
    readonly timeoutMs?: number;
    readonly responses?: readonly unknown[];
    readonly auth?: RemoteOptions["auth"];
  } = {},
): Promise<Drill> {
  const r = remote("refunds", CARD_URL, {
    transport: p,
    timeoutMs: options.timeoutMs ?? 1,
    ...(options.auth === undefined ? {} : { auth: options.auth }),
  });
  const tools = r.tools({ name: TOOL, description: "Ask the refunds desk." });
  const specs = tools.map((t) => t.spec());
  const h = await harness(specs, [], options.responses ?? [CALL, DONE]);
  const impls = new Map(
    tools.map((t) => {
      const impl = t.bind({
        deps: undefined,
        threadId: THREAD,
        branchId: ROOT,
        principal: { issuer: "api", tenant: "acme", subject: "alice" },
      });
      return [impl.spec.name, impl];
    }),
  );
  const base = h.config;
  const kills: Promise<unknown>[] = [];
  return {
    ...h,
    specs,
    kills,
    config: (o = {}) => ({ ...base({ tools: impls }), ...o }),
    restart: async (afterMs = 60_000) => {
      await Promise.all(kills.splice(0));
      h.clock.now += afterMs;
      return unwrap(await h.store.acquire(ROOT, "restarted", 30_000));
    },
  };
}

/**
 * Another owner takes the lease the moment `type` commits, so the dying writer's next fence or
 * append is refused. `onEvent` is not awaited by the loop, so the takeover is handed to `restart`.
 */
export function killAfter(
  h: Drill,
  type: KnownEvent["type"],
): (e: KnownEvent) => void {
  return (e) => {
    if (e.type !== type) return;
    h.clock.now += 60_000;
    // A one-millisecond lease is enough: what stops the dead owner is the new epoch, not the lease.
    h.kills.push(h.store.acquire(ROOT, "usurper", 1));
  };
}

/** A tools_changed that drops `name` from the set, hashed as the rule requires. */
export function without(specs: readonly ToolSpec[], name: string): EventDraft {
  const tools = specs.filter((t) => t.name !== name);
  const json = JsonValue.parse(tools);
  const text = canonicalize(json);
  if (!text.ok) throw new Error("a tool set is canonical JSON");
  return {
    type: "tools_changed",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: { tools: [...tools], tools_hash: sha256Hex(text.value) },
  };
}

/** A cancel another process recorded, with the principal its schema requires. */
export const CANCELLED: EventDraft = {
  type: "cancel_requested",
  type_version: 1,
  critical: true,
  actor: {
    kind: "user",
    principal: { issuer: "api", tenant: "acme", subject: "alice" },
  },
  data: { scope: "turn" },
};

/** What another process appended while this branch was down. */
export async function appendAs(
  h: Drill,
  drafts: readonly EventDraft[],
): Promise<void> {
  await Promise.all(h.kills.splice(0));
  h.clock.now += 60_000;
  const other = unwrap(await h.store.acquire(ROOT, "other", 1));
  unwrap(await other.append([...drafts]));
}

export function types(events: readonly KnownEvent[]): readonly string[] {
  return events.map((e) => e.type);
}

/** The last event of a type, for an assertion that reads one field of its data. */
export function lastOf<T extends KnownEvent["type"]>(
  events: readonly KnownEvent[],
  type: T,
): Extract<KnownEvent, { type: T }> | undefined {
  return events.findLast(
    (e): e is Extract<KnownEvent, { type: T }> => e.type === type,
  );
}
