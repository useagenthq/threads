import type { AskId, MemberRef } from "../../log";
import { askRoom } from "../../loop/ledger";
import { openAsk } from "../../team/ask";
import { requestCancel } from "../../team/cancel";
import { TEAM_CONSTANTS } from "../../team/constants";
import { refTarget } from "../../team/operator";
import type { ObserveRefusal } from "../../team/results";
import { openWait, waitMembers } from "../../team/watch";
import { recipientOf } from "./budgets";
import { drive } from "./drive";
import type {
  AskStatus,
  TeamAskOptions,
  TeamAskResult,
  TeamCancelResult,
  TeamWaitOptions,
  TeamWaitResult,
} from "./handle-types";
import {
  type HandleEnv,
  isIn,
  KEYED,
  limitsOf,
  operator,
} from "./operator-request";
import { askOutcome, askStatus, waitOutcome } from "./outcomes";
import { BUSY } from "./team-log";

// team.ask, team.wait, team.cancel and team.askStatus (spec/api.json Team): ask, wait and cancel
// are operator requests decided by the model tools' ops; ask and wait then return the outcome the
// team log records, driving the team until it does (drive.ts). askStatus is a pure read.

export async function askMember(
  env: HandleEnv,
  to: MemberRef,
  question: string,
  options: TeamAskOptions,
): Promise<TeamAskResult> {
  const { idempotencyKey, timeoutMs } = options;
  const recipient = recipientOf(env.log, env.artifacts);
  const done = await operator(
    env,
    "ask",
    { to, question, timeout_ms: timeoutMs },
    idempotencyKey,
    (req, team) =>
      openAsk(req, refTarget(req.tx, team, to), question, {
        limits: limitsOf(env),
        headroom: async (row) => {
          const got = await recipient(req.tx, row);
          return got === undefined || askRoom(got, req.tx);
        },
        ...(timeoutMs === undefined ? {} : { timeoutMs }),
      }),
  );
  if (done === BUSY) return { status: "refused", code: BUSY };
  if (done.status === "refused" && isIn(ASK_CODES, done.code))
    return { status: "refused", code: done.code };
  if (done.status !== "open") throw new Error(`an ask recorded ${done.status}`);
  return drive(env, () => askOutcome(env, env.ref.id, done.ask_id));
}

export async function waitFor(
  env: HandleEnv,
  members: readonly MemberRef[],
  options: TeamWaitOptions,
): Promise<TeamWaitResult> {
  const { idempotencyKey, mode, timeoutMs } = options;
  const distinct = waitMembers(members, mode);
  // Refused before any writer, so, like busy, it records nothing.
  if (distinct === "invalid_request")
    return { status: "refused", code: distinct };
  // The wait's id is its request's mail id; a replay re-attaches with the first request's.
  let opened = "";
  const done = await operator(
    env,
    "wait",
    { members: [...members], mode, timeout_ms: timeoutMs },
    idempotencyKey,
    (req, team, close) => {
      opened = req.mailId;
      const targets = distinct.map((m) => refTarget(req.tx, team, m));
      return openWait(req, close, targets, {
        mode: mode ?? "all",
        timeoutMs: timeoutMs ?? TEAM_CONSTANTS.askWaitDefaultMs,
      });
    },
  );
  if (done === BUSY) return { status: "refused", code: BUSY };
  if (done.status === "refused" && isIn(WAIT_CODES, done.code))
    return { status: "refused", code: done.code };
  if (done.status !== "waiting" && done.status !== "waited")
    throw new Error(
      `a wait recorded ${done.status}${done.status === "refused" ? `{${done.code}}` : ""}`,
    );
  const waitId = done.status === "waiting" ? done.wait_id : opened;
  return drive(env, () => waitOutcome(env, env.ref.id, waitId));
}

export async function cancelMember(
  env: HandleEnv,
  member: MemberRef,
  idempotencyKey: string | undefined,
): Promise<TeamCancelResult> {
  const done = await operator(
    env,
    "cancel",
    { member },
    idempotencyKey,
    (req, team) => requestCancel(req, refTarget(req.tx, team, member)),
  );
  if (done === BUSY) return { status: "refused", code: BUSY };
  if (done.status === "cancel_requested") return done;
  if (done.status === "refused" && isIn(CANCEL_CODES, done.code))
    return { status: "refused", code: done.code };
  throw new Error(`a cancel recorded ${done.status}`);
}

export function statusOf(env: HandleEnv, askId: AskId): Promise<AskStatus> {
  return askStatus(env, env.ref.id, askId);
}

type AskCode = Extract<TeamAskResult, { status: "refused" }>["code"];
type WaitCode = Extract<TeamWaitResult, { status: "refused" }>["code"];
type CancelCode = Extract<TeamCancelResult, { status: "refused" }>["code"];

const OBSERVE: readonly ObserveRefusal[] = [
  "forbidden",
  "unknown_member",
  "stale_member",
];
const ASK_CODES: readonly AskCode[] = [
  ...OBSERVE,
  "member_ended",
  "lead",
  "mailbox_full",
  "team_closed",
  "budget_exceeded",
  ...KEYED,
];
const WAIT_CODES: readonly WaitCode[] = [...OBSERVE, ...KEYED];
const CANCEL_CODES: readonly CancelCode[] = [
  ...OBSERVE,
  "member_ended",
  ...KEYED,
];
