import { sha256Hex } from "../../src/hash";
import type { LoopExtension } from "../../src/hooks/types";
import type { EventId, KnownEvent, Policy } from "../../src/log";
import { type LoopConfig, resume } from "../../src/loop";
import { compactionSide, renderFrom } from "../../src/render";
import type { EventDraft } from "../../src/store";
import { ROOT, unwrap, userInput } from "../store/helpers";
import { events, type Harness, harness } from "./harness";

// A requested compaction at the loop: a finished turn, compaction_requested, and the next run's
// input already durable, so resuming the branch carries the request out first.

const principal = { issuer: "api", tenant: "acme", subject: "operator" };
const usage = { input_tokens: 10, output_tokens: 2 };
export const SUMMARY_TEXT = "The user said first.";

export function reply(text: string): Readonly<Record<string, unknown>> {
  return { content: [{ type: "text", text }], stop_reason: "end_turn", usage };
}
export function fail(
  reason: string,
  status: number,
): Readonly<Record<string, unknown>> {
  return { error: { reason, http_status: status } };
}

export const requested = (instructions?: string): EventDraft => ({
  type: "compaction_requested",
  type_version: 1,
  critical: true,
  actor: { kind: "user", principal },
  data: instructions === undefined ? {} : { instructions },
});

const turnDone: EventDraft = {
  type: "turn_completed",
  type_version: 1,
  critical: true,
  actor: { kind: "host" },
  data: { reason: "end_turn" },
};

/** first input and its end, the request, then the next input: the log a crash left. */
export async function asked(
  responses: readonly unknown[],
  policy?: Policy,
  before: readonly EventDraft[] = [],
  idle: readonly EventDraft[] = [],
): Promise<Harness> {
  return harness(
    [],
    [
      userInput("first"),
      ...before,
      turnDone,
      ...idle,
      requested("Keep the numbers."),
      userInput("next"),
    ],
    responses,
    undefined,
    policy,
  );
}

/** Resumes the branch under a new lease; returns what this owner appended. */
export async function resumeOnce(
  h: Harness,
  config: Partial<LoopConfig> = {},
): Promise<readonly KnownEvent[]> {
  h.clock.now += 60_000;
  const writer = unwrap(await h.store.acquire(ROOT, `owner-${h.clock.now}`));
  const seq = writer.chain.fold.seq;
  await resume(writer, h.artifacts, h.config(config));
  await writer.release();
  return events(writer).filter((e) => e.seq > seq);
}

/** Appends drafts built from the current log, as the crashed owner did. */
export async function crashed(
  h: Harness,
  build: (
    log: readonly KnownEvent[],
  ) => readonly EventDraft[] | Promise<readonly EventDraft[]>,
): Promise<void> {
  h.clock.now += 1;
  const writer = unwrap(await h.store.acquire(ROOT, `crashed-${h.clock.now}`));
  unwrap(await writer.append(await build(events(writer))));
  await writer.release();
}

export function requestOf(log: readonly KnownEvent[]): EventId {
  const found = log.findLast((e) => e.type === "compaction_requested");
  if (found === undefined) throw new Error("a compaction request");
  return found.event_id;
}

/** The side request the crashed owner sent: its bytes stored as Render v1 makes them. */
export async function side(
  h: Harness,
  log: readonly KnownEvent[],
  attempt: number,
): Promise<EventDraft> {
  const cause = requestOf(log);
  const rendered = await renderFrom(
    log,
    h.artifacts,
    compactionSide(log, cause),
  );
  const { bytes, prefix } = unwrap(rendered);
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
        sha256: await h.artifacts.put(bytes),
        bytes: bytes.length,
        media_type: "application/x-ndjson",
      },
      declared_prefix: { bytes: prefix.length, sha256: sha256Hex(prefix) },
    },
  };
}

/** How the side request's latest attempt ended, recorded before the crash. */
export function abandon(
  log: readonly KnownEvent[],
  reason: "crash" | "prompt_too_long" | "rate_limited" | "provider_error",
): EventDraft {
  const last = log.findLast((e) => e.type === "model_request");
  if (last === undefined) throw new Error("a side request");
  return {
    type: "model_attempt_abandoned",
    type_version: 1,
    critical: true,
    actor: { kind: reason === "crash" ? "recovery" : "host" },
    data: {
      request_event_id: last.event_id,
      provider_outcome: reason === "crash" ? "unknown" : "failed",
      reason,
    },
  };
}

/** A before_compact extension that answers `guide` with its name and counts its calls. */
export function guide(name: string, calls: Map<string, number>): LoopExtension {
  return {
    name,
    timeoutMs: 1000,
    hooks: {
      before_compact: async () => {
        calls.set(name, (calls.get(name) ?? 0) + 1);
        return { decision: "guide", text: `guide from ${name}` };
      },
    },
  };
}

/** Compaction side requests among `log`. */
export const sides = (log: readonly KnownEvent[]): readonly KnownEvent[] =>
  log.filter(
    (e) => e.type === "model_request" && e.data.purpose === "compaction",
  );

export const outcome = (log: readonly KnownEvent[]): KnownEvent | undefined =>
  log.find((e) => e.type === "compacted" || e.type === "compaction_failed");
