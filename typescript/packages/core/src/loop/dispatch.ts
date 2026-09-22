import { assertNever } from "../assert-never";
import type { EventOf } from "../fold/state";
import { effectKey } from "../fold/state";
import { sha256Hex } from "../hash";
import { canonicalize } from "../log";
import { draft, TOOL } from "./drafts";
import { FINAL_OUTPUT, validateCandidate } from "./output";
import type { Session } from "./session";
import { settleUnknown } from "./settle";
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
    const stopped = await runCall(s, call);
    if (stopped !== undefined || s.fold.parked.length > 0) return stopped;
  }
  return undefined;
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
    const decided = s.config.authorize(call, s.fold);
    return s.append(
      draft.permission({
        call_id: callId,
        decision: decided.decision,
        source: decided.source,
        ...(decided.rule_id === undefined ? {} : { rule_id: decided.rule_id }),
      }),
    );
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
  if (name === FINAL_OUTPUT) return validateCandidate(s, call);
  const spec = toolSpec(s.fold, name);
  if (spec?.effect_class === "read_only") {
    const run = await body(s, call);
    return run.kind === "done" ? result(s, callId, run) : undefined;
  }
  const attempts = s.events.filter(
    (e) => e.type === "effect_begin" && e.data.call_id === callId,
  ).length;
  const begun = s.append(
    draft.effectBegin({ call_id: callId, attempt: attempts + 1 }),
  );
  if (begun !== undefined) return begun;
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
      principal: s.config.principal,
      signal: s.config.signal ?? new AbortController().signal,
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
      const ref = s.store(run.output, "text/plain");
      return (
        s.append(
          draft.effectCommit({
            call_id: callId,
            result_ref: ref,
            ...(run.receipt === undefined
              ? {}
              : { provider_receipt: run.receipt }),
          }),
        ) ?? result(s, callId, run)
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
): Halt | undefined {
  return s.append(
    draft.toolResult(
      {
        call_id: callId,
        is_error: run.isError,
        origin: "executed",
        preview: run.output,
      },
      TOOL,
    ),
  );
}
