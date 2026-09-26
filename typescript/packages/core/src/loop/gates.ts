import { assertNever } from "../assert-never";
import type { EventOf } from "../fold/state";
import type { Outcome } from "../hooks/invoke";
import type { HookReturns } from "../hooks/types";
import type { KnownEvent } from "../log";
import type { EventDraft } from "../store";
import { draft } from "./drafts";
import { context, decision, defining, injection, recorded, run } from "./hooks";
import type { Session } from "./session";
import { stepEvents, turnEvents } from "./turn";
import type { Halt } from "./types";

// The gates in front of a turn request: the input guardrail, the
// tool-output guardrail, after_tool_batch and before_model. Each decision is recorded with
// what it causes in one append, before the request that it gates, so a recovered run reads the
// decision and never runs the hook again.

/** The gate ended the turn; the loop moves on to the next step. */
export type Gated = Halt | "ended" | undefined;

const encoder = new TextEncoder();

/** before_input on the turn's input, before the turn's first request. */
export async function inputGate(s: Session): Promise<Gated> {
  const turn = turnEvents(s.events, s.fold);
  const input = turn[0];
  if (input?.type !== "user_input") return undefined;
  if (turn.some((e) => e.type === "model_request")) return undefined;
  const key = { input_event_id: input.event_id };
  for (const ext of defining(s, "before_input")) {
    if (recorded(turn, ext.name, "before_input", key) !== undefined) continue;
    const out = await run(ext, "before_input", [input.data]);
    const refused = refusal(out);
    if (refused !== undefined) {
      const { decided, reason } = refused;
      return (
        (await s.append(
          decision(ext.name, "before_input", decided, key, reason),
          draft.turnCompleted("input_denied"),
        )) ?? "ended"
      );
    }
    if (out.kind === "failed" || out.value.decision === "deny") continue;
    const stopped = await s.append(
      decision(ext.name, "before_input", "allow", key),
      ...(out.value.injections ?? []).map((t) => injection(ext.name, t)),
    );
    if (stopped !== undefined) return stopped;
  }
  return undefined;
}

/**
 * before_tool_result on each executed result of this step. redact and deny change only what
 * later requests render (context_edited{guardrail}); the raw result stays in the log.
 */
export async function resultsGate(s: Session): Promise<Halt | undefined> {
  const step = stepEvents(s.events, s.fold);
  const results = step.filter(
    (e): e is EventOf<"tool_result"> =>
      e.type === "tool_result" && e.data.origin === "executed",
  );
  for (const result of results) {
    const key = { call_id: result.data.call_id };
    const call = s.events.findLast(
      (e) => e.type === "tool_call" && e.data.call_id === key.call_id,
    );
    if (call?.type !== "tool_call") continue;
    for (const ext of defining(s, "before_tool_result")) {
      if (recorded(step, ext.name, "before_tool_result", key) !== undefined)
        continue;
      const out = await run(
        ext,
        "before_tool_result",
        [call.data, result.data],
        key.call_id,
      );
      const stopped = await s.append(...guarded(ext.name, result, out));
      if (stopped !== undefined) return stopped;
    }
  }
  return undefined;
}

type ResultOutcome =
  | { readonly kind: "ok"; readonly value: HookReturns["before_tool_result"] }
  | { readonly kind: "failed"; readonly reason: string };

function guarded(
  ext: string,
  result: EventOf<"tool_result">,
  out: ResultOutcome,
): readonly EventDraft[] {
  const key = { call_id: result.data.call_id };
  const clear = (decided: "deny" | "failed", reason: string) => [
    decision(ext, "before_tool_result", decided, key, reason),
    draft.contextEdited({
      reason: "guardrail",
      edits: [{ call_id: key.call_id, action: "clear" }],
    }),
  ];
  if (out.kind === "failed") return clear("failed", out.reason);
  const value = out.value;
  switch (value.decision) {
    case "proceed":
      return [decision(ext, "before_tool_result", "proceed", key)];
    case "deny":
      return clear("deny", value.reason);
    case "redact": {
      const part = textPart(result);
      if (part === undefined || !value.spans.every((x) => inside(part.text, x)))
        return clear("failed", "redaction spans outside the result's text");
      return [
        decision(ext, "before_tool_result", "redact", key),
        draft.contextEdited({
          reason: "guardrail",
          edits: [
            {
              call_id: key.call_id,
              action: "redact",
              part: part.index,
              spans: [...value.spans],
            },
          ],
        }),
      ];
    }
    default:
      return assertNever(value);
  }
}

/** The result's first text part: the preview when it has no content (render rule). */
function textPart(
  result: EventOf<"tool_result">,
): { readonly index: number; readonly text: string } | undefined {
  const { content, preview } = result.data;
  if (content === undefined)
    return preview === undefined ? undefined : { index: 0, text: preview };
  const index = content.findIndex((p) => p.type === "text");
  const part = content[index];
  return part?.type === "text" ? { index, text: part.text } : undefined;
}

/** A span inside the text's UTF-8 bytes, on character boundaries (semantic rule 19). */
function inside(text: string, span: { start: number; end: number }): boolean {
  const bytes = encoder.encode(text);
  const boundary = (at: number): boolean =>
    at === bytes.length || ((bytes[at] ?? 0) & 0xc0) !== 0x80;
  return (
    span.start >= 0 &&
    span.start < span.end &&
    span.end <= bytes.length &&
    boundary(span.start) &&
    boundary(span.end)
  );
}

/** after_tool_batch once every result of the last response is recorded; a failure ends the turn. */
export async function batchGate(s: Session): Promise<Gated> {
  const step = stepEvents(s.events, s.fold);
  if (!step.some((e) => e.type === "tool_result")) return undefined;
  // Keyed by the response whose calls this batch answered, so the decision names its subject and
  // a re-run of the step reads its own record back (parity with Python's after_batch).
  const response = turnEvents(s.events, s.fold).findLast(
    (e) => e.type === "model_response",
  );
  if (response?.type !== "model_response") return undefined;
  const key = { request_event_id: response.data.request_event_id };
  const got = await context(s, "after_tool_batch", [s.state()], step, key);
  if (got === "failed")
    return (await s.append(draft.turnCompleted("error"))) ?? "ended";
  return got;
}

/** before_model per attempt: proceed with its injections, or the attempt isn't sent. */
export async function modelGate(s: Session): Promise<Gated> {
  const window = sinceLastRequest(stepEvents(s.events, s.fold));
  for (const ext of defining(s, "before_model")) {
    if (recorded(window, ext.name, "before_model") !== undefined) continue;
    const out = await run(ext, "before_model", [s.state()]);
    const refused = refusal(out);
    if (refused !== undefined) {
      const { decided, reason } = refused;
      return (
        (await s.append(
          decision(ext.name, "before_model", decided, {}, reason),
          draft.turnCompleted("error"),
        )) ?? "ended"
      );
    }
    if (out.kind === "failed" || out.value.decision === "deny") continue;
    const stopped = await s.append(
      decision(ext.name, "before_model", "proceed"),
      ...(out.value.injections ?? []).map((t) => injection(ext.name, t)),
    );
    if (stopped !== undefined) return stopped;
  }
  return undefined;
}

/** A gate's refusal: the hook failed (it denies) or denied. */
function refusal(
  out: Outcome<"before_input" | "before_model">,
):
  | { readonly decided: "failed" | "deny"; readonly reason: string }
  | undefined {
  if (out.kind === "failed") return { decided: "failed", reason: out.reason };
  return out.value.decision === "deny"
    ? { decided: "deny", reason: out.value.reason }
    : undefined;
}

function sinceLastRequest(step: readonly KnownEvent[]): readonly KnownEvent[] {
  return step.slice(step.findLastIndex((e) => e.type === "model_request") + 1);
}
