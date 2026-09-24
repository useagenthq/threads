import type { EventOf } from "../../fold/state";
import {
  type Json,
  MailId,
  type MemberRef,
  type Principal,
  ThreadId,
} from "../../log";
import { startPin } from "../../loop/agents/start-pin";
import { startRoom } from "../../loop/ledger";
import { redactSecrets } from "../../redact/text";
import { knownEvents } from "../../reduce";
import type { LogStore } from "../../store";
import type { ArtifactStore } from "../../store/artifacts";
import { uuidv7 } from "../../store/encode";
import type { DecideTx } from "../../store/writer";
import type { Batch, Mint } from "../../team/batch";
import { TEAM_LIMITS } from "../../team/constants";
import {
  type OperatorOp,
  openOperator,
  type Recorded,
  refTarget,
} from "../../team/operator";
import { type StartPlan, send, start } from "../../team/ops";
import type { Request } from "../../team/request";
import { memberRows, type TeamRow, teamRow } from "../../team/rows";
import type { DeferTools } from "../defer";
import type { MemberEntry } from "../registry";
import { ancestorsOf } from "./budgets";
import { teamEvents } from "./feed";
import type {
  Team,
  TeamRef,
  TeamSendResult,
  TeamStartOptions,
  TeamStartResult,
} from "./handle-types";
import { roster } from "./roster";
import { pins } from "./runtime";
import { BUSY, onTeamLog } from "./team-log";

// The operator's handle (spec/api.json Team): each of start and send is one operator request
// decided in one team-log append (design §4.4), by the same ops as the model's tools; members and
// events are pure reads. Lane 21E adds ask, wait, cancel and askStatus as further requests.

export type HandleEnv = {
  readonly log: LogStore;
  readonly artifacts: ArtifactStore;
  readonly ref: TeamRef;
  readonly principal: Principal;
  /** The lead's definition in this process: the agents start resolves. Undefined: none here. */
  readonly lead: MemberEntry | undefined;
  readonly busyBoundMs?: number;
  readonly mint?: Mint;
};

const utf8 = new TextEncoder();

export function teamHandle(env: HandleEnv): Team {
  return {
    ref: env.ref,
    start: (agent, task, options = {}) =>
      startMember(env, agent, task, options),
    send: (to, text, options = {}) =>
      sendTo(env, to, text, options.idempotencyKey),
    members: async () => roster(env.log, env.artifacts, env.ref.id),
    events: (options = {}) => teamEvents(env.log, env.ref.id, options.after),
  };
}

async function startMember(
  env: HandleEnv,
  agent: string,
  task: string,
  options: TeamStartOptions,
): Promise<TeamStartResult> {
  const { idempotencyKey, ...chosen } = options;
  const args = { agent, task, ...chosen };
  const lead = leadOf(env);
  const pin = pins(env.lead?.team ?? [], lead.deferTools);
  const { pinned, resolved } = await startPin(
    pin,
    env.artifacts,
    args,
    "operator",
  );
  const plan: StartPlan = {
    agents: listed(agent, pinned?.configHash),
    resolved,
    limits: env.lead?.teamLimits ?? TEAM_LIMITS,
    // The lead is the new member's parent: its budgets and its ancestors' cover the member.
    headroom: () =>
      pinned !== undefined &&
      startRoom(env.log.budgets, ancestorsOf(env.log, lead.parent), pinned),
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
    (req, team) => send(req, refTarget(req.db, team, to), text, limitsOf(env)),
  );
  if (done === BUSY) return { status: "refused", code: BUSY };
  if (done.status === "sent")
    return { status: "sent", id: MailId.parse(done.id) };
  if (done.status === "refused" && isIn(SEND_CODES, done.code))
    return { status: "refused", code: done.code };
  throw new Error(`a send recorded ${done.status}`);
}

/**
 * One operator request under the team-log writer: its key looked up, then `decide` with the
 * op. Returns what the op or the key's replay recorded, or busy.
 */
async function operator(
  env: HandleEnv,
  op: OperatorOp,
  body: Readonly<Record<string, Json | undefined>>,
  idempotencyKey: string | undefined,
  decide: (req: Request, team: TeamRow) => Recorded,
): Promise<Recorded | typeof BUSY> {
  const team = teamRow(env.log.driver, env.ref.id);
  if (team === undefined) throw new Error(`no team ${env.ref.id}`);
  const requestId = uuidv7(env.log.now());
  return onTeamLog(
    env.log,
    team.team_log_branch_id,
    (tx: DecideTx, batch: Batch) => {
      // Read in the append: the team may have closed since the handle looked.
      const now = teamRow(tx.db, env.ref.id) ?? team;
      const opened = openOperator(
        { db: tx.db, chain: tx.chain, batch, put: putText(env), team: now },
        {
          requestId,
          op,
          principal: env.principal,
          body: present(body),
          idempotencyKey,
        },
      );
      return opened.kind === "recorded"
        ? opened.outcome
        : decide(opened.request, now);
    },
    {
      ...(env.busyBoundMs === undefined
        ? {}
        : { busyBoundMs: env.busyBoundMs }),
      ...(env.mint === undefined ? {} : { mint: env.mint }),
    },
  );
}

/** A body's parameters as api.json names them, an omitted option left out. */
function present(
  body: Readonly<Record<string, Json | undefined>>,
): Readonly<Record<string, Json>> {
  return Object.fromEntries(
    Object.entries(body).flatMap(([k, v]) => (v === undefined ? [] : [[k, v]])),
  );
}

/** A text above the inline cap, stored before the append that names it (redacted, C5). */
function putText(env: HandleEnv): Request["put"] {
  return (text) => {
    const bytes = utf8.encode(redactSecrets(text));
    return {
      sha256: env.artifacts.put(bytes),
      bytes: bytes.length,
      media_type: "text/plain",
    };
  };
}

const limitsOf = (env: HandleEnv) => env.lead?.teamLimits ?? TEAM_LIMITS;

function listed(
  agent: string,
  configHash: string | undefined,
): StartPlan["agents"] {
  return new Map(configHash === undefined ? [] : [[agent, { configHash }]]);
}

/** The lead as a member's parent, and the defer_tools its members inherit. */
function leadOf(env: HandleEnv): {
  readonly parent: NonNullable<EventOf<"thread_started">["data"]["parent"]>;
  readonly deferTools: DeferTools | undefined;
} {
  const row = memberRows(env.log.driver, env.ref.id).find(
    (r) => r.role === "lead",
  );
  const read =
    row?.branch_id === undefined || row.branch_id === null
      ? undefined
      : env.log.read(row.branch_id);
  if (row === undefined || read === undefined || !read.ok)
    throw new Error(`team ${env.ref.id} has no readable lead log`);
  const started = knownEvents(read.value).find(
    (e) => e.type === "thread_started",
  );
  if (started?.type !== "thread_started" || row.branch_id === null)
    throw new Error("a lead log starts with thread_started");
  return {
    parent: {
      thread_id: row.thread_id,
      branch_id: row.branch_id,
      event_id: started.event_id,
      relation: "team_member",
    },
    deferTools: started.data.policy?.context?.defer_tools,
  };
}

type StartCode = Extract<TeamStartResult, { status: "refused" }>["code"];
type SendCode = Extract<TeamSendResult, { status: "refused" }>["code"];

// The codes the team log can record for each op (its key refusals included).
const KEYED = [
  "idempotency_key_reused",
  "idempotency_key_principal_mismatch",
] as const;
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

const isIn = <C extends string>(codes: readonly C[], code: string): code is C =>
  codes.some((c) => c === code);
