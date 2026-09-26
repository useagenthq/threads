import { z } from "zod";
import type { EventOf } from "../../fold/state";
import {
  type BranchId,
  JsonObject,
  type KnownEvent,
  ModelRef,
  Policy,
  ThreadId,
} from "../../log";
import type { Covering, TeamRecipient } from "../../loop";
import { knownEvents } from "../../reduce";
import type { ArtifactStore, LogStore } from "../../store";
import { READ_ONLY, type Tx } from "../../store/driver";
import { type MemberRow, mailEnvelope, teamRow } from "../../team/rows";

// A member's budgets (spec/schema/README.md, "Teams"; design §2.6): before every model request of
// a member turn the ledger reserves against the member's own thread budget, every ancestor
// thread's budget through its structural parents (team_member, subagent, handoff) up to the root,
// and the run budget of that turn's root request.

type Parent = Pick<
  NonNullable<EventOf<"thread_started">["data"]["parent"]>,
  "thread_id" | "branch_id"
>;

/** The member_started a member's thread_started.parent names. */
type Starter = Parent & { readonly event_id: string };

/**
 * The cap a start put on the member (Team.start's budget, capped by the messagePolicy rule's):
 * member_started.budget, a budget of the member's own thread beside the one its pin carries. The
 * pin is hashed, so a per-start cap can only live here.
 */
export async function startedCap(
  log: LogStore,
  parent: Starter | undefined,
): Promise<readonly Covering[]> {
  if (parent === undefined) return [];
  const read = await log.read(parent.branch_id);
  if (!read.ok) return [];
  const started = knownEvents(read.value).find(
    (e) => e.type === "member_started" && e.event_id === parent.event_id,
  );
  if (started?.type !== "member_started") return [];
  const budget = started.data.budget;
  return budget === undefined
    ? []
    : [{ budgetId: `start:${started.event_id}`, budget, scope: "thread" }];
}

/** Every ancestor thread's own budget, from the member's parent up to the root. */
export async function ancestorsOf(
  log: LogStore,
  parent: Parent | undefined,
): Promise<readonly Covering[]> {
  const out: Covering[] = [];
  for (let at = parent; at !== undefined; ) {
    const read = await log.read(at.branch_id);
    if (!read.ok) break;
    const started = knownEvents(read.value).find(
      (e) => e.type === "thread_started",
    );
    if (started?.type !== "thread_started") break;
    const budget = started.data.policy?.budget;
    if (budget !== undefined)
      out.push({
        budgetId: `thread:${at.thread_id}`,
        budget,
        owner: at.thread_id,
        scope: "ancestor",
      });
    at = started.data.parent;
  }
  return out;
}

const Pinned = z.object({
  model: ModelRef,
  model_params: JsonObject,
  policy: Policy.optional(),
});
type Pinned = z.infer<typeof Pinned>;

/**
 * An asked member's budgets (ask's headroom): its own thread budget and every ancestor's, with
 * what one request of its model reserves, from its thread_started; a member still starting has
 * no log yet, so from its pinned config, under its lead. Read in `tx`, the ask's append.
 */
export function recipientOf(
  store: LogStore,
  artifacts: ArtifactStore,
): (tx: Tx, row: MemberRow) => Promise<TeamRecipient | undefined> {
  return async (tx, row) => {
    const log = store.within(tx);
    const got =
      row.branch_id === null
        ? await configured(log, tx, artifacts, row)
        : await started(log, row.branch_id);
    if (got === undefined) return undefined;
    const own = got.pinned.policy?.budget;
    return {
      model: got.pinned.model,
      params: got.pinned.model_params,
      policy: got.pinned.policy,
      covering: [
        ...(own === undefined
          ? []
          : [
              {
                budgetId: `thread:${row.thread_id}`,
                budget: own,
                scope: "thread" as const,
              },
            ]),
        ...got.cap,
        ...(await ancestorsOf(log, got.parent)),
      ],
    };
  };
}

type Recipient = {
  readonly pinned: Pinned;
  readonly parent: Parent | undefined;
  /** The member's start cap; empty while it is still starting (its start checked it already). */
  readonly cap: readonly Covering[];
};

async function started(
  log: LogStore,
  branch: BranchId,
): Promise<Recipient | undefined> {
  const read = await log.read(branch);
  const e = read.ok
    ? knownEvents(read.value).find((x) => x.type === "thread_started")
    : undefined;
  if (e?.type !== "thread_started") return undefined;
  return {
    pinned: e.data,
    parent: e.data.parent,
    cap: await startedCap(log, e.data.parent),
  };
}

async function configured(
  log: LogStore,
  tx: Tx,
  artifacts: ArtifactStore,
  row: MemberRow,
): Promise<Recipient | undefined> {
  const bytes = await artifacts.get(row.config_hash);
  const team = await teamRow(tx, row.team_id);
  const lead =
    team === undefined ? undefined : await log.mainBranch(team.lead_thread_id);
  if (!bytes.ok || team === undefined || lead === undefined || !lead.ok)
    return undefined;
  const pinned = Pinned.parse(
    JSON.parse(new TextDecoder().decode(bytes.value)),
  );
  return {
    pinned,
    parent: { thread_id: team.lead_thread_id, branch_id: lead.value },
    cap: [],
  };
}

/**
 * The run budget of the request a member turn belongs to: the root request of its opener's
 * provenance (a receipt's, or its task's), when that request is a user_input with a budget. An
 * operator request's run budget only attributes in Phase 1.
 */
export function runCovering(
  log: LogStore,
): (opener: KnownEvent) => Promise<Covering | undefined> {
  const found = new Map<string, Covering | undefined>();
  return async (opener) => {
    const root = await rootOf(log, opener);
    if (root === undefined) return undefined;
    const key = `run:${root.thread_id}:${root.event_id}`;
    if (!found.has(key)) found.set(key, await budgetOf(log, root, key));
    return found.get(key);
  };
}

async function rootOf(
  log: LogStore,
  opener: KnownEvent,
): Promise<
  { readonly thread_id: string; readonly event_id: string } | undefined
> {
  if (opener.type === "message_received")
    return opener.data.envelope.provenance.root_request;
  if (opener.type !== "user_input" || opener.data.mail_id === undefined)
    return undefined;
  const envelope = await log.driver.transaction(
    (tx) => mailEnvelope(tx, opener.data.mail_id ?? ""),
    READ_ONLY,
  );
  return envelope?.provenance.root_request;
}

async function budgetOf(
  log: LogStore,
  root: { readonly thread_id: string; readonly event_id: string },
  budgetId: string,
): Promise<Covering | undefined> {
  const branch = await log.mainBranch(ThreadId.parse(root.thread_id));
  const read = branch.ok ? await log.read(branch.value) : undefined;
  if (read === undefined || !read.ok) return undefined;
  const input = knownEvents(read.value).find(
    (e) => e.event_id === root.event_id,
  );
  const budget = input?.type === "user_input" ? input.data.budget : undefined;
  return budget === undefined ? undefined : { budgetId, budget, scope: "run" };
}
