import {
  type Agent,
  type AgentOptions,
  agent,
  openThread,
  type Store,
  scriptedModel,
  sqlite,
  type Thread,
  type ThreadRef,
} from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { sha256Hex } from "../../src/hash";
import type { EventId, KnownEvent } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { compactionSide, refReader, render } from "../../src/render";
import type { EventDraft } from "../../src/store";
import { unwrap } from "../store/helpers";

// Shared by the Thread.replay, compact and setOutputStyle tests: scripted runs, the operator
// who controls them, and ways to read and write their logs.

const usage = { input_tokens: 10, output_tokens: 2 };
export const operator = { issuer: "api", tenant: "local", subject: "operator" };
export const SUMMARY = "The user said hello; the agent answered.";
export const KEEP = "Keep the open invoice numbers.";

/** A model script entry. */
export type Reply = Readonly<Record<string, unknown>>;

export function say(text: string): Reply {
  return { content: [{ type: "text", text }], stop_reason: "end_turn", usage };
}
export function use(name: string, input: object, id: string): Reply {
  const part = { type: "tool_use", call_id: id, name, input };
  return { content: [part], stop_reason: "tool_use", usage };
}
export function rejected(reason: string, status = 400): Reply {
  return { error: { reason, http_status: status } };
}

type Options = Omit<AgentOptions<undefined, string>, "model" | "output">;

/** A thread with one finished turn, its agent, and the handle an operator opens. */
export async function finished(
  script: readonly Reply[],
  options: Options = {},
): Promise<{
  readonly store: Store;
  readonly bot: Agent;
  readonly ref: ThreadRef;
  readonly thread: Thread;
}> {
  const store = sqlite(":memory:");
  const bot = agent({
    ...options,
    model: scriptedModel({ responses: script }),
  });
  const first = await bot.run("hello", { store });
  if (first.status !== "completed")
    throw new Error(`first run ${first.status}`);
  const thread = unwrap(await openThread(store, first.thread.id));
  return { store, bot, ref: first.thread, thread };
}

/** The branch's known events, read through the store. */
export async function events(
  ref: Pick<ThreadRef, "store" | "branch">,
): Promise<readonly KnownEvent[]> {
  const { log } = await openStore(ref.store);
  return knownEvents(unwrap(log.read(ref.branch)));
}

/** The text of a recorded request's bytes. */
export async function requestText(
  store: Store,
  request: KnownEvent,
): Promise<string> {
  if (request.type !== "model_request") throw new Error("not a request");
  const { artifacts } = await openStore(store);
  const bytes = unwrap(artifacts.get(request.data.request_ref.sha256));
  return new TextDecoder().decode(bytes);
}

/** Appends drafts under a lease of their own, as a process that then crashed would have. */
export async function append(
  ref: Pick<ThreadRef, "store" | "branch">,
  ...drafts: readonly EventDraft[]
): Promise<void> {
  const { log } = await openStore(ref.store);
  const writer = unwrap(log.acquire(ref.branch, "crashed"));
  try {
    unwrap(writer.append(drafts));
  } finally {
    writer.release();
  }
}

/** The side request a crashed run would have made for `cause`: its bytes stored, as sent. */
export async function sideRequest(
  ref: Pick<ThreadRef, "store" | "branch">,
  cause: EventId,
  attempt: number,
): Promise<EventDraft> {
  const { artifacts } = await openStore(ref.store);
  const before = await events(ref);
  const side = compactionSide(before, cause);
  const { bytes, prefix } = unwrap(render(before, refReader(artifacts), side));
  return {
    type: "model_request",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      attempt,
      purpose: "compaction",
      cause_event_id: cause,
      request_ref: {
        sha256: artifacts.put(bytes),
        bytes: bytes.length,
        media_type: "application/x-ndjson",
      },
      declared_prefix: { bytes: prefix.length, sha256: sha256Hex(prefix) },
    },
  };
}

/** A recorded abandonment of `request`, as recovery or the live path writes it. */
export function abandoned(
  request: EventId,
  reason: "crash" | "prompt_too_long" | "rate_limited" | "provider_error",
): EventDraft {
  return {
    type: "model_attempt_abandoned",
    type_version: 1,
    critical: true,
    actor: { kind: reason === "crash" ? "recovery" : "host" },
    data: {
      request_event_id: request,
      provider_outcome: reason === "crash" ? "unknown" : "failed",
      reason,
    },
  };
}

/** The last event of `type`, typed. */
export function last<T extends KnownEvent["type"]>(
  log: readonly KnownEvent[],
  type: T,
): Extract<KnownEvent, { type: T }> {
  const found = log.findLast(
    (e): e is Extract<KnownEvent, { type: T }> => e.type === type,
  );
  if (found === undefined) throw new Error(`no ${type} in the log`);
  return found;
}
