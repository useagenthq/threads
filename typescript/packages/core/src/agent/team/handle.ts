import { MailId, type MemberRef, ThreadId } from "../../log";
import { listedOf, startPin } from "../../loop/agents/start-pin";
import { startRoom } from "../../loop/ledger";
import { uuidv7 } from "../../store/encode";
import { refTarget } from "../../team/operator";
import { type StartPlan, send, start } from "../../team/ops";
import { ancestorsOf } from "./budgets";
import { teamEvents } from "./feed";
import type {
  Team,
  TeamSendResult,
  TeamStartOptions,
  TeamStartResult,
} from "./handle-types";
import {
  type HandleEnv,
  isIn,
  KEYED,
  leadOf,
  limitsOf,
  operator,
} from "./operator-request";
import { roster } from "./roster";
import { pins } from "./runtime";
import { BUSY } from "./team-log";
import { askMember, cancelMember, statusOf, waitFor } from "./waits";

// The operator's handle (spec/api.json Team): each of start, send, ask, wait and cancel is one
// operator request decided in one team-log append (design §4.4), by the same ops as the model's
// tools (waits.ts has ask, wait and cancel); members, events and askStatus are pure reads.

export type { HandleEnv } from "./operator-request";

export function teamHandle(env: HandleEnv): Team {
  return {
    ref: env.ref,
    start: (agent, task, options = {}) =>
      startMember(env, agent, task, options),
    send: (to, text, options = {}) =>
      sendTo(env, to, text, options.idempotencyKey),
    ask: (to, question, options = {}) => askMember(env, to, question, options),
    wait: (members, options = {}) => waitFor(env, members, options),
    cancel: (member, options = {}) =>
      cancelMember(env, member, options.idempotencyKey),
    askStatus: (askId) => statusOf(env, askId),
    members: () => roster(env.log, env.artifacts, env.ref.id),
    events: (options = {}) => teamEvents(env.log, env.ref.id, options),
  };
}

async function startMember(
  env: HandleEnv,
  agent: string,
  task: string,
  options: TeamStartOptions,
): Promise<TeamStartResult> {
  const { idempotencyKey, budget, ...chosen } = options;
  const args = {
    agent,
    task,
    ...chosen,
    ...(budget === undefined ? {} : { budget }),
  };
  const lead = await leadOf(env.log, env.ref.id);
  // Read before the append: the lead's and its ancestors' budgets cover the new member.
  const starter = await ancestorsOf(env.log, lead.parent);
  const pin = pins(env.lead.team ?? [], lead.deferTools);
  const { pinned, resolved } = await startPin(
    pin,
    env.artifacts,
    args,
    "operator",
  );
  const plan: StartPlan = {
    agents: listedOf(agent, pinned, budget),
    resolved,
    limits: limitsOf(env),
    // The lead is the new member's parent: its budgets and its ancestors' cover the member.
    headroom: async (_agent, tx) =>
      pinned !== undefined && (await startRoom(starter, pinned, tx, budget)),
    threadId: ThreadId.parse(uuidv7(env.log.now())),
  };
  const done = await operator(env, "start", args, idempotencyKey, (req) =>
    start(req, args, plan),
  );
  if (done === BUSY) return { status: "refused", code: BUSY };
  if (done.status === "started") return done;
  if (done.status === "refused" && isIn(START_CODES, done.code))
    return {
      status: "refused",
      code: done.code,
      ...(done.detail === undefined ? {} : { detail: done.detail }),
    };
  throw new Error(`a start recorded ${done.status}`);
}

async function sendTo(
  env: HandleEnv,
  to: MemberRef,
  text: string,
  idempotencyKey: string | undefined,
): Promise<TeamSendResult> {
  const done = await operator(
    env,
    "send",
    { to, text },
    idempotencyKey,
    (req, team) => send(req, refTarget(req.tx, team, to), text, limitsOf(env)),
  );
  if (done === BUSY) return { status: "refused", code: BUSY };
  if (done.status === "sent")
    return { status: "sent", id: MailId.parse(done.id) };
  if (done.status === "refused" && isIn(SEND_CODES, done.code))
    return { status: "refused", code: done.code };
  throw new Error(`a send recorded ${done.status}`);
}

type StartCode = Extract<TeamStartResult, { status: "refused" }>["code"];
type SendCode = Extract<TeamSendResult, { status: "refused" }>["code"];

// The codes the team log can record for each op (its key refusals included).
const START_CODES: readonly StartCode[] = [
  "forbidden",
  "unknown_agent",
  "concurrency_cap",
  "budget_exceeded",
  "team_closed",
  "invalid_definition",
  ...KEYED,
];
const SEND_CODES: readonly SendCode[] = [
  "forbidden",
  "unknown_member",
  "stale_member",
  "member_ended",
  "self",
  "mailbox_full",
  "team_closed",
  ...KEYED,
];
