import { assertNever } from "../assert-never";
import type { EventOf } from "../fold/state";
import { effectKey } from "../fold/state";
import { sha256Hex } from "../hash";
import { canonicalize } from "../log";
import { err, ok } from "../result";
import type { EventDraft } from "../store";
import { authorize } from "./authorize";
import { draft, TOOL } from "./drafts";
import { frameworkTool } from "./framework";
import { observe } from "./hooks";
import type { Session } from "./session";
import { settleUnknown } from "./settle";
import { type Recorded, recordOutput } from "./spill";
import { toolSpec } from "./turn";
import type { Halt, ToolRun } from "./types";

// Pending calls, in call order: authorization first, then the body. An effect's
// effect_begin is durable (and fenced by the lease in the same transaction) before dispatch,
// and a dispatched attempt stays potentially sent until it is settled.

const HOUR = 3_600_000;

export async function runCalls(s: Session): Promise<Halt | undefined> {
  for (const callId of [...s.fold.pending]) {
    const call = s.events.findLast(
      (e) => e.type === "tool_call" && e.data.call_id === callId,
    );
    if (call?.type !== "tool_call") continue;
    const before = s.fold.seq;
    const stopped =
      (await runCall(s, call)) ?? (await afterTool(s, call, before));
    // A step that parked or ended the turn (a handoff) dispatches nothing more.
    if (stopped !== undefined || s.fold.parked.length > 0 || !s.fold.turnOpen)
      return stopped;
  }
  return undefined;
}

/** after_tool observes a result this call just recorded; it can't deny or undo the effect. */
async function afterTool(
  s: Session,
  call: EventOf<"tool_call">,
  before: number,
): Promise<Halt | undefined> {
  const result = s.fold.calls.get(call.data.call_id)?.result;
  if (
    result?.type !== "tool_result" ||
    result.seq <= before ||
    result.data.origin !== "executed"
  )
    return undefined;
  return observe(s, "after_tool", [call.data, result.data], {
    call_id: call.data.call_id,
  });
}

async function runCall(
  s: Session,
  call: EventOf<"tool_call">,
): Promise<Halt | undefined> {
  const { call_id: callId } = call.data;
  const decision = s.events.findLast(
    (e) => e.type === "permission_decision" && e.data.call_id === callId,
  );
  if (decision?.type !== "permission_decision") {
    // Recorded, then the process stopped before authorizing it: authorize it now.
    return authorize(s, call);
  }
  if (s.fold.calls.get(callId)?.allowed === true) return dispatch(s, call);
  if (decision.data.decision === "deny")
    return denied(s, callId, decision.data.reason);
  return ask(s, call);
}

function denied(s: Session, callId: string, reason?: string): Halt | undefined {
  return s.append(
    draft.toolResult(
      {
        call_id: callId,
        is_error: true,
        origin: "denied",
        preview: `denied${reason === undefined ? "" : `: ${reason}`}`,
      },
      { kind: "host" },
    ),
  );
}

/** An ask opens an approval challenge and parks until an approver answers. */
function ask(s: Session, call: EventOf<"tool_call">): Halt | undefined {
  const { call_id: callId, input } = call.data;
  // A granted challenge allowed the call, so an answered one here was denied: never ask again.
  if (
    [...s.fold.approvals.values()].some(
      (a) => a.callId === callId && a.consumed,
    )
  )
    return denied(s, callId, "approval denied");
  const open = [...s.fold.approvals].find(
    ([, a]) => a.callId === callId && !a.consumed,
  );
  if (open !== undefined)
    return s.append(
      draft.parked({
        address: { kind: "approval", id: open[0] },
        reason: "awaiting_approval",
      }),
    );
  const args = canonicalize(input);
  if (!args.ok) throw new Error("tool_call input is canonical JSON");
  const challenge = crypto.randomUUID();
  return (
    s.append(
      draft.approvalRequested({
        challenge_id: challenge,
        call_id: callId,
        args_hash: sha256Hex(args.value),
        expires_at: s.now() + HOUR,
      }),
    ) ??
    s.append(
      draft.parked({
        address: { kind: "approval", id: challenge },
        reason: "awaiting_approval",
      }),
    )
  );
}

async function dispatch(
  s: Session,
  call: EventOf<"tool_call">,
): Promise<Halt | undefined> {
  const { name, call_id: callId } = call.data;
  const framework = frameworkTool(name);
  if (framework !== undefined) return framework(s, call);
  const spec = toolSpec(s.fold, name);
  if (spec?.effect_class === "read_only") {
    const fenced = s.fence();
    if (fenced !== undefined) return fenced;
    const run = await body(s, call);
    // A read_only call changes nothing, so an uncertain run is just a failed one.
    const done: Extract<ToolRun, { kind: "done" }> =
      run.kind === "done"
        ? run
        : { kind: "done", output: `failed: ${run.kind}`, isError: true };
    return s.append(result(s, callId, done), ...injections(done));
  }
  const attempts = s.events.filter(
    (e) => e.type === "effect_begin" && e.data.call_id === callId,
  ).length;
  const begun = s.append(
    draft.effectBegin({ call_id: callId, attempt: attempts + 1 }),
  );
  if (begun !== undefined) return begun;
  // Fenced in the same synchronous section as the dispatch: a stale owner never runs it.
  const fenced = s.fence();
  if (fenced !== undefined) return fenced;
  const run = await sent(s, call);
  return settle(s, callId, run);
}

/** The body of a mediated operation, or its stub in stub mode. */
async function sent(
  s: Session,
  call: EventOf<"tool_call">,
): Promise<ToolRun | "unmatched"> {
  const stub = s.config.stub;
  if (stub === undefined) return body(s, call);
  const args = canonicalize(call.data.input);
  if (!args.ok) throw new Error("tool_call input is canonical JSON");
  const answer = stub.answer(call.data.name, sha256Hex(args.value));
  return answer === undefined
    ? "unmatched"
    : { kind: "done", output: answer.output, isError: answer.isError };
}

async function body(s: Session, call: EventOf<"tool_call">): Promise<ToolRun> {
  const impl = s.config.tools.get(call.data.name);
  if (impl === undefined)
    return {
      kind: "done",
      output: `no implementation for ${call.data.name}`,
      isError: true,
    };
  const key = effectKey(s.fold, call.data.call_id, s.branchId);
  try {
    return await impl.run(call.data.input, {
      effectKey: key,
      callId: call.data.call_id,
      branchId: s.branchId,
      epoch: s.epoch,
      principal: s.config.principal,
      signal: s.config.signal ?? new AbortController().signal,
      fence: async () => {
        const halted = s.fence();
        return halted === undefined
          ? ok(undefined)
          : err({ code: "stale_epoch", message: halted.message });
      },
    });
  } catch {
    // Anything after dispatch but a result is uncertainty, never a plain error.
    return { kind: "unknown", reason: "transport_error" };
  }
}

function settle(
  s: Session,
  callId: string,
  run: ToolRun | "unmatched",
): Promise<Halt | undefined> | Halt | undefined {
  if (run === "unmatched") {
    const stopped = s.append(
      draft.effectResolved(
        { call_id: callId, outcome: "not_sent", by: "adapter" },
        { kind: "host" },
      ),
    );
    return (
      stopped ?? {
        code: "unmatched_external_op",
        message: `no stub for ${callId}`,
      }
    );
  }
  switch (run.kind) {
    case "done": {
      const shown = recordOutput(s, callId, run.output);
      const ref = shown.ref ?? s.store(shown.text, "text/plain");
      // The result artifact is durable first; the commit and its result land together.
      return s.append(
        draft.effectCommit({
          call_id: callId,
          result_ref: ref,
          ...(run.receipt === undefined
            ? {}
            : { provider_receipt: run.receipt }),
        }),
        resultOf(callId, run.isError, shown),
        ...injections(run),
      );
    }
    case "unknown":
      return (
        s.append(
          draft.effectUnknown({ call_id: callId, reason: run.reason }),
        ) ?? settleUnknown(s, callId, { kind: "host" })
      );
    case "not_sent":
      return (
        s.append(
          draft.effectResolved(
            { call_id: callId, outcome: "not_sent", by: "adapter" },
            { kind: "host" },
          ),
        ) ??
        s.append(
          draft.toolResult(
            {
              call_id: callId,
              is_error: true,
              origin: "not_executed",
              preview: "not sent",
            },
            { kind: "host" },
          ),
        )
      );
    default:
      return assertNever(run);
  }
}

function result(
  s: Session,
  callId: string,
  run: Extract<ToolRun, { kind: "done" }>,
): EventDraft {
  return resultOf(callId, run.isError, recordOutput(s, callId, run.output));
}

/** The context a result brings, after it and before the next request (C6). */
function injections(run: Extract<ToolRun, { kind: "done" }>): EventDraft[] {
  return (run.inject ?? []).map((d) => draft.injected(d));
}

function resultOf(
  callId: string,
  isError: boolean,
  shown: Recorded,
): EventDraft {
  return draft.toolResult(
    {
      call_id: callId,
      is_error: isError,
      origin: "executed",
      preview: shown.preview,
      ...(shown.ref === undefined ? {} : { ref: shown.ref }),
    },
    TOOL,
  );
}
