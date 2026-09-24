import { assertNever } from "../assert-never";
import type { Outcome } from "../hooks/invoke";
import { recordCalls } from "./calls";
import { draft, HOST } from "./drafts";
import { decision, defining, instruction, recorded, run } from "./hooks";
import { finish } from "./lifecycle";
import { missingCandidate } from "./output";
import { contextPolicy } from "./policy";
import { endTurn } from "./request";
import type { Session } from "./session";
import { type Response, turnEvents } from "./turn";
import type { Halt } from "./types";

// What a recorded response leads to: after_model first, then its calls or
// the turn's ending (spec/schema/README.md, "Turn endings by stop_reason").

const CONTINUE =
  "Output limit reached. Continue exactly where you stopped. Do not repeat earlier output.";
/** after_model retries per turn before a retry counts as a deny. */
const MAX_RETRIES = 2;

type Verdict =
  | { readonly kind: "proceed" }
  | {
      readonly kind: "deny" | "guide";
      readonly ext: string;
      readonly text: string;
    };

export async function respond(
  s: Session,
  response: Response,
): Promise<Halt | undefined> {
  const verdict = await afterModel(s, response);
  if (verdict === undefined || "code" in verdict) return verdict;
  switch (verdict.kind) {
    case "proceed":
      return proceed(s, response);
    case "deny":
      return withheld(s, response, `denied: ${verdict.text}`, undefined);
    case "guide":
      return withheld(
        s,
        response,
        `not dispatched: ${verdict.ext} asked for another answer`,
        instruction(verdict.ext, verdict.text),
      );
    default:
      return assertNever(verdict);
  }
}

async function proceed(
  s: Session,
  response: Response,
): Promise<Halt | undefined> {
  if (response.data.content.some((p) => p.type === "tool_use"))
    return recordCalls(s, response);
  const stop = response.data.stop_reason;
  switch (stop) {
    case "end_turn":
    case "stop_sequence":
    case "refusal":
    case "tool_use":
      return s.fold.policy?.output === undefined
        ? finish(s)
        : missingCandidate(s);
    case "max_tokens":
      return continuation(s);
    case "context_window_exceeded":
      return endTurn(s, "context_exhausted");
    // A pause within the cap re-requests before respond (turn.ts); here it is past the cap.
    case "pause_turn":
    case "other":
      return endTurn(s, "error");
    default:
      return assertNever(stop);
  }
}

/**
 * after_model gates the response's undispatched calls and the release of its output. The
 * verdict is read back from recorded decisions, so a recovered run applies the same one.
 */
async function afterModel(
  s: Session,
  response: Response,
): Promise<Verdict | Halt | undefined> {
  const key = { request_event_id: response.data.request_event_id };
  const after = s.events.filter((e) => e.seq > response.seq);
  for (const ext of defining(s, "after_model")) {
    const prior = recorded(after, ext.name, "after_model", key);
    if (prior !== undefined) {
      const verdict = standing(ext.name, prior.data);
      if (verdict.kind !== "proceed") return verdict;
      continue;
    }
    const out = await run(ext, "after_model", [s.state(), response.data]);
    const [decided, reason] = decide(s, out);
    const stopped = s.append(
      decision(ext.name, "after_model", decided, key, reason),
    );
    if (stopped !== undefined) return stopped;
    const verdict = standing(ext.name, { decision: decided, reason });
    if (verdict.kind !== "proceed") return verdict;
  }
  return { kind: "proceed" };
}

function decide(
  s: Session,
  out: Outcome<"after_model">,
): readonly ["proceed" | "deny" | "guide" | "retry" | "failed", string?] {
  if (out.kind === "failed") return ["failed", out.reason];
  const v = out.value;
  switch (v.decision) {
    case "proceed":
      return ["proceed"];
    case "deny":
      return ["deny", v.reason];
    case "guide":
      return ["guide", v.text];
    case "retry": {
      const retries = turnEvents(s.events, s.fold).filter(
        (e) =>
          e.type === "hook_decision" &&
          e.data.hook === "after_model" &&
          e.data.decision === "retry",
      ).length;
      return retries < MAX_RETRIES
        ? ["retry", v.reason]
        : ["deny", `retry limit reached: ${v.reason}`];
    }
    default:
      return assertNever(v);
  }
}

/** A recorded decision's effect: retry re-asks with its reason, like guide. */
function standing(
  ext: string,
  d: { readonly decision: string; readonly reason?: string | undefined },
): Verdict {
  const text = d.reason ?? "";
  switch (d.decision) {
    case "proceed":
      return { kind: "proceed" };
    case "guide":
    case "retry":
      return { kind: "guide", ext, text };
    default:
      return { kind: "deny", ext, text };
  }
}

/**
 * The output is withheld: each call is recorded and closed without dispatch, so the pairs stay
 * whole, then either the guide instruction re-asks or, with nothing to re-ask, the turn ends.
 */
function withheld(
  s: Session,
  response: Response,
  preview: string,
  guide: ReturnType<typeof instruction> | undefined,
): Halt | undefined {
  const closed = response.data.content.flatMap((p) =>
    p.type === "tool_use"
      ? [
          draft.toolCall({
            call_id: p.call_id,
            name: p.name,
            input: p.input,
            request_event_id: response.data.request_event_id,
          }),
          draft.toolResult(
            { call_id: p.call_id, is_error: true, origin: "denied", preview },
            HOST,
          ),
        ]
      : [],
  );
  if (guide !== undefined) return s.append(...closed, guide);
  if (closed.length > 0) return s.append(...closed);
  return endTurn(s, "error");
}

/** ask to continue, up to max_output_continuations per turn, then max_output. */
function continuation(s: Session): Halt | undefined {
  const asked = turnEvents(s.events, s.fold).filter(
    (e) => e.type === "injected" && e.data.text === CONTINUE,
  ).length;
  if (asked >= contextPolicy(s.fold.policy).max_output_continuations)
    return endTurn(s, "max_output");
  return s.append(
    draft.injected({
      source: "recovery",
      trust: "trusted_instruction",
      origin: { id: "max_output" },
      text: CONTINUE,
    }),
  );
}
