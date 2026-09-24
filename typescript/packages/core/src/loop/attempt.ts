import { AssertionError } from "node:assert";
import { assertNever } from "../assert-never";
import { type EventOf, responseText } from "../fold/state";
import { sha256Hex } from "../hash";
import type { EventId, OutputPart, Usage } from "../log";
import { assertModelAllowed, type Model, type ModelChunk } from "../model";
import { type Unsupported, unsupported } from "../model/capabilities";
import type { ProviderRejection } from "../model/protocol";
import { parseRender } from "../model/render-lines";
import { redactStream, SecretInProviderOutput } from "../redact";
import { compactionSide, refReader, render } from "../render";
import type { EventDraft } from "../store";
import { StoreError } from "../store/driver";
import { draft } from "./drafts";
import { reserve, settleOpen } from "./ledger";
import type { Session } from "./session";
import { cancelRequested } from "./turn";
import type { Halt } from "./types";

// One model attempt: render, store the request bytes,
// append model_request (durable before dispatch), then exactly one send.

type Rejection = Extract<ModelChunk, { kind: "rejected" }>;
/** A provider rejection class: what the retry, fallback and compaction rules act on. */
type Provider = Rejection & { readonly reason: ProviderRejection };

export type Attempted =
  | { readonly kind: "response"; readonly text: string }
  | { readonly kind: "rejected"; readonly rejection: Provider }
  | { readonly kind: "broken" }
  /** Refused before sending, by the loop's pre-check or the adapter (8). */
  | { readonly kind: "unsupported"; readonly refused: Unsupported }
  /**
   * The response held a registered secret in provider material that can't be redacted (C5):
   * nothing of it is stored. `ended`: the turn ended with secret_in_provider_output; false
   * when a cancel was already requested, which closes the turn instead.
   */
  | { readonly kind: "leaked"; readonly ended: boolean }
  /**
   * A covering budget refused the reservation: budget_exceeded is recorded, with a side
   * request's compaction_failed in the same batch.
   */
  | { readonly kind: "budget" }
  /**
   * A cancel barrier is in the open turn: nothing was sent. A side request's compaction_failed
   * is recorded; the cancellation step is next.
   */
  | { readonly kind: "barred" }
  | { readonly kind: "halt"; readonly halt: Halt };

type Collected =
  | {
      readonly kind: "done";
      readonly parts: readonly OutputPart[];
      readonly stop: EventOf<"model_response">["data"]["stop_reason"];
      readonly usage: Usage;
    }
  | { readonly kind: "rejected"; readonly rejection: Rejection }
  | { readonly kind: "broken" }
  | { readonly kind: "leaked" };

const halt = (code: Halt["code"], message: string): Attempted => ({
  kind: "halt",
  halt: { code, message },
});

/**
 * `cause` is the compaction_requested a side request summarizes for: the request names it, and
 * its history ends there (Render v1).
 */
export async function attempt(
  s: Session,
  purpose: "turn" | "compaction",
  number: number,
  cause?: EventId,
): Promise<Attempted> {
  // The one barrier check for every model request: from here to the append nothing awaits, so
  // no cancel can land in between (spec/schema/README.md, "Nothing new after a barrier").
  if (cancelRequested(s.events) !== undefined) return barred(s, purpose, cause);
  const model = s.fold.model && s.config.models(s.fold.model);
  if (model === undefined)
    return halt("model_error", "no adapter for this settings epoch's model");
  const events = s.events;
  const side =
    purpose === "compaction" ? compactionSide(events, cause) : undefined;
  const rendered = render(events, refReader(s.artifacts), side);
  if (!rendered.ok)
    return halt(
      rendered.error.code === "artifact_missing"
        ? "artifact_missing"
        : "artifact_corrupt",
      rendered.error.message,
    );
  const { bytes, prefix } = rendered.value;
  const refused = unsupported(parseRender(bytes), model.info);
  if (refused !== undefined) return { kind: "unsupported", refused };
  // Reserved right before the request is appended, at the seq it will take.
  const over = reserve(s);
  if (over !== undefined) return refuse(s, over, purpose, cause);
  const tags = purpose === "compaction" ? sideTags(cause) : {};
  const stopped = s.append(
    draft.modelRequest({
      attempt: number,
      ...tags,
      request_ref: s.store(bytes, "application/x-ndjson"),
      declared_prefix: { bytes: prefix.length, sha256: sha256Hex(prefix) },
    }),
  );
  if (stopped !== undefined) return { kind: "halt", halt: stopped };
  const requestId = s.events.at(-1)?.event_id ?? "";
  // Fenced in the same synchronous section as the send: a stale owner never sends.
  const fenced = s.fence();
  if (fenced !== undefined) return { kind: "halt", halt: fenced };
  const collected = await collect(s, model, requestId, bytes);
  const recorded = record(s, requestId, collected, cause);
  settleOpen(s);
  return recorded;
}

const sideTags = (
  cause: EventId | undefined,
): { readonly purpose: "compaction"; readonly cause_event_id?: EventId } =>
  cause === undefined
    ? { purpose: "compaction" }
    : { purpose: "compaction", cause_event_id: cause };

/** A side request that is never sent owes its compaction_failed; a turn request owes nothing. */
function owed(
  purpose: "turn" | "compaction",
  cause: EventId | undefined,
): readonly EventDraft[] {
  if (purpose === "turn") return [];
  return [
    draft.compactionFailed({
      stage: "summary",
      reason: "model_error",
      ...(cause === undefined ? {} : { cause_event_id: cause }),
    }),
  ];
}

/** Nothing is sent after a cancel barrier: a side request's failure, and nothing else. */
function barred(
  s: Session,
  purpose: "turn" | "compaction",
  cause: EventId | undefined,
): Attempted {
  const answer = owed(purpose, cause);
  const stopped = answer.length === 0 ? undefined : s.append(...answer);
  return stopped === undefined
    ? { kind: "barred" }
    : { kind: "halt", halt: stopped };
}

/** budget_exceeded; a side request's compaction_failed goes in the same batch. */
function refuse(
  s: Session,
  over: Parameters<typeof draft.budgetExceeded>[0],
  purpose: "turn" | "compaction",
  cause: EventId | undefined,
): Attempted {
  const stopped = s.append(draft.budgetExceeded(over), ...owed(purpose, cause));
  return stopped === undefined
    ? { kind: "budget" }
    : { kind: "halt", halt: stopped };
}

async function collect(
  s: Session,
  model: Model,
  requestId: string,
  body: Uint8Array,
): Promise<Collected> {
  assertModelAllowed(model);
  const parts: OutputPart[] = [];
  const shown = redactStream();
  const request = { request_id: `${s.branchId}:${requestId}`, body };
  const options =
    s.config.signal === undefined ? {} : { signal: s.config.signal };
  try {
    for await (const chunk of model.send(request, s.modelContext(), options)) {
      switch (chunk.kind) {
        case "delta":
          delta(s, requestId, shown.feed(chunk.text));
          break;
        case "part":
          parts.push(chunk.part);
          break;
        case "done":
          delta(s, requestId, shown.end());
          return {
            kind: "done",
            parts,
            stop: chunk.stop_reason,
            usage: chunk.usage,
          };
        case "rejected":
          return { kind: "rejected", rejection: chunk };
        default:
          assertNever(chunk);
      }
    }
  } catch (error) {
    // A broken invariant, or the store's outage: the request stays open, for recovery (as in
    // Python's `_collect`), never a resend under the crash budget.
    if (error instanceof AssertionError || error instanceof StoreError)
      throw error;
    if (error instanceof SecretInProviderOutput) return { kind: "leaked" };
    // A transport failure after dispatch: the attempt may have been billed.
    return { kind: "broken" };
  }
  return { kind: "broken" };
}

/** A delta shown to a streaming caller: redacted, never logged. */
function delta(s: Session, requestId: string, text: string): void {
  if (text !== "") s.config.onDelta?.(requestId, text);
}

function record(
  s: Session,
  requestId: string,
  c: Collected,
  cause: EventId | undefined,
): Attempted {
  switch (c.kind) {
    case "done": {
      const stopped = s.append(
        draft.modelResponse({
          request_event_id: requestId,
          content: [...c.parts],
          stop_reason: c.stop,
          usage: c.usage,
          completeness: "complete",
        }),
      );
      if (stopped !== undefined) return { kind: "halt", halt: stopped };
      // As recorded (redacted): a summary made from it is stored too.
      const recorded = s.events.at(-1);
      if (recorded?.type !== "model_response")
        throw new Error("model_response was just appended");
      return { kind: "response", text: responseText(recorded.data.content) };
    }
    case "rejected":
      return rejected(s, requestId, c.rejection);
    case "broken": {
      const stopped = s.append(
        draft.abandoned({
          request_event_id: requestId,
          provider_outcome: "unknown",
          reason: "stream_broken",
        }),
      );
      return stopped === undefined ? c : { kind: "halt", halt: stopped };
    }
    case "leaked": {
      // The response arrived (it may be billed) but none of it is kept. The abandonment and
      // the turn's end are one batch: a crash between them can't leave the turn open. A
      // requested compaction's side request is answered in it too, before the turn closes.
      const answered =
        cause === undefined
          ? []
          : [
              draft.compactionFailed({
                stage: "summary",
                reason: "model_error",
                request_event_id: requestId,
                cause_event_id: cause,
              }),
            ];
      // A cancel already in the log is the turn's end: the cancellation step records it.
      const ended = cancelRequested(s.events) === undefined;
      const stopped = s.append(
        draft.abandoned({
          request_event_id: requestId,
          provider_outcome: "unknown",
          reason: "provider_error",
        }),
        ...answered,
        ...(ended
          ? [draft.turnCompleted("error", "secret_in_provider_output")]
          : []),
      );
      return stopped === undefined
        ? { kind: "leaked", ended }
        : { kind: "halt", halt: stopped };
    }
    default:
      return assertNever(c);
  }
}

/**
 * The send's terminal error (spec/api.json Model.send returns.errors). A fence refusal appends
 * nothing: this writer lost its lease. An adapter refusal was never sent, so it is not_sent and
 * ends the turn with its code; it is never an unknown outcome that gets re-sent.
 */
function rejected(s: Session, requestId: string, r: Rejection): Attempted {
  const { reason, http_status, retry_after_ms, billing } = r;
  switch (reason) {
    case "stale_epoch":
      return halt("branch_busy", "the fence refused the send: lease lost");
    case "content_unsupported":
    case "continuation_unsupported":
    case "transport_fence_unsupported": {
      const stopped = s.append(
        draft.abandoned({
          request_event_id: requestId,
          provider_outcome: "not_sent",
          reason: "provider_error",
        }),
      );
      if (stopped !== undefined) return { kind: "halt", halt: stopped };
      const message =
        reason === "transport_fence_unsupported"
          ? "the adapter refused to send: its transport bypasses the fence"
          : "the adapter refused a rendered part before sending";
      return { kind: "unsupported", refused: { code: reason, message } };
    }
    default: {
      const stopped = s.append(
        draft.abandoned({
          request_event_id: requestId,
          provider_outcome: "failed",
          reason,
          ...(http_status === undefined ? {} : { http_status }),
          ...(retry_after_ms === undefined ? {} : { retry_after_ms }),
          ...(billing === undefined ? {} : { billing }),
        }),
      );
      return stopped === undefined
        ? { kind: "rejected", rejection: { ...r, reason } }
        : { kind: "halt", halt: stopped };
    }
  }
}
