import { MailId, type MemberRef, ThreadId } from "../../log";
import { listedOf, startPin } from "../../loop/agents/start-pin";
import { startRoom } from "../../loop/ledger";
import { uuidv7 } from "../../store/encode";
import { refTarget } from "../../team/operator";
import { type StartPlan, send, start } from "../../team/ops";
import { restart } from "../../team/supervise";
import { ancestorsOf } from "./budgets";
import { teamEvents } from "./feed";
import type {
  Team,
  TeamSendResult,
  TeamStartOptions,
  TeamStartResult,
} from "./handle-types";
import { pinHostMember } from "./host-member";
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
  task: string | undefined,
  options: TeamStartOptions,
): Promise<TeamStartResult> {
  // A host team is leadless: host({members}) starts its members, and the only start an operator
  // makes there is the restart of one the supervisor stopped (Teams Phase 2, E).
  if (env.lead === undefined)
    return await restartMember(env, agent, options.idempotencyKey);
  // A lead's member is its task: there is nothing to start it with. Before any writer, so
  // nothing is recorded.
  if (task === undefined) return { status: "refused", code: "invalid_request" };
  const lead0 = env.lead;
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
  const pin = pins(lead0.team ?? [], lead.deferTools);
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

/**
 * The operator's restart on a host team: the next generation of a host member the supervisor
 * stopped, in a new empty thread, recorded as an operator request like any other (rule 51).
 */
async function restartMember(
  env: HandleEnv,
  name: string,
  idempotencyKey: string | undefined,
): Promise<TeamStartResult> {
  const threadId = ThreadId.parse(uuidv7(env.log.now()));
  // Re-pinned before the append: an operator restarts a stopped member to pick up the agent as
  // it is registered now, which is the whole point of restarting it by hand.
  const entry = env.agents?.get(name);
  const configHash =
    entry === undefined ? undefined : await pinHostMember(env.artifacts, entry);
  const done = await operator(
    env,
    "start",
    { agent: name },
    idempotencyKey,
    (req, _team, close) =>
      restart(req, name, close.chain.fold, threadId, configHash),
  );
  if (done === BUSY) return { status: "refused", code: BUSY };
  if (done.status === "started") return done;
  if (done.status === "refused" && isIn(START_CODES, done.code))
    return { status: "refused", code: done.code };
  throw new Error(`a restart recorded ${done.status}`);
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
