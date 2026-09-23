import { assertNever } from "../assert-never";
import { type EventOf, responseText } from "../fold/state";
import { sha256Hex } from "../hash";
import type { OutputPart, Usage } from "../log";
import { assertModelAllowed, type Model, type ModelChunk } from "../model";
import { type Unsupported, unsupported } from "../model/capabilities";
import type { ProviderRejection } from "../model/protocol";
import { parseRender } from "../model/render-lines";
import { compactionInstruction, refReader, render } from "../render";
import { draft } from "./drafts";
import { reserve, settleOpen } from "./ledger";
import type { Session } from "./session";
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
  /** Refused before sending, by the loop's pre-check or the adapter. */
  | { readonly kind: "unsupported"; readonly refused: Unsupported }
  /** A covering budget refused the reservation; budget_exceeded is recorded. */
  | { readonly kind: "budget" }
  | { readonly kind: "halt"; readonly halt: Halt };

type Collected =
  | {
      readonly kind: "done";
      readonly parts: readonly OutputPart[];
      readonly stop: EventOf<"model_response">["data"]["stop_reason"];
      readonly usage: Usage;
    }
  | { readonly kind: "rejected"; readonly rejection: Rejection }
  | { readonly kind: "broken" };

const halt = (code: Halt["code"], message: string): Attempted => ({
  kind: "halt",
  halt: { code, message },
});

export async function attempt(
  s: Session,
  purpose: "turn" | "compaction",
  number: number,
): Promise<Attempted> {
  const model = s.fold.model && s.config.models(s.fold.model);
  if (model === undefined)
    return halt("model_error", "no adapter for this settings epoch's model");
  const events = s.events;
  const instruction =
    purpose === "compaction" ? compactionInstruction(events) : undefined;
  const rendered = render(events, refReader(s.artifacts), instruction);
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
  if (over !== undefined) {
    const stopped = s.append(draft.budgetExceeded(over));
    return stopped === undefined
      ? { kind: "budget" }
      : { kind: "halt", halt: stopped };
  }
  const stopped = s.append(
    draft.modelRequest({
      attempt: number,
      ...(purpose === "compaction" ? { purpose } : {}),
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
  const recorded = record(s, requestId, collected);
  settleOpen(s);
  return recorded;
}

async function collect(
  s: Session,
  model: Model,
  requestId: string,
  body: Uint8Array,
): Promise<Collected> {
  assertModelAllowed(model);
  const parts: OutputPart[] = [];
  const request = { request_id: `${s.branchId}:${requestId}`, body };
  const options =
    s.config.signal === undefined ? {} : { signal: s.config.signal };
  try {
    for await (const chunk of model.send(request, s.modelContext(), options)) {
      switch (chunk.kind) {
        case "delta":
          s.config.onDelta?.(requestId, chunk.text);
          break;
        case "part":
          parts.push(chunk.part);
          break;
        case "done":
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
  } catch {
    // A transport failure after dispatch: the attempt may have been billed.
    return { kind: "broken" };
  }
  return { kind: "broken" };
}

function record(s: Session, requestId: string, c: Collected): Attempted {
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
      return { kind: "response", text: responseText([...c.parts]) };
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
