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
import { type MemberRow, mailEnvelope, teamRow } from "../../team/rows";

// A member's budgets (spec/schema/README.md, "Teams"; design §2.6): before every model request of
// a member turn the ledger reserves against the member's own thread budget, every ancestor
// thread's budget through its structural parents (team_member, subagent, handoff) up to the root,
// and the run budget of that turn's root request.

type Parent = Pick<
  NonNullable<EventOf<"thread_started">["data"]["parent"]>,
  "thread_id" | "branch_id"
>;

/** Every ancestor thread's own budget, from the member's parent up to the root. */
export function ancestorsOf(
  log: LogStore,
  parent: Parent | undefined,
): readonly Covering[] {
  const out: Covering[] = [];
  for (let at = parent; at !== undefined; ) {
    const read = log.read(at.branch_id);
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
 * no log yet, so from its pinned config, under its lead.
 */
export function recipientOf(
  log: LogStore,
  artifacts: ArtifactStore,
): (row: MemberRow) => TeamRecipient | undefined {
  return (row) => {
    const got =
      row.branch_id === null
        ? configured(log, artifacts, row)
        : started(log, row.branch_id);
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
        ...ancestorsOf(log, got.parent),
      ],
    };
  };
}

function started(
  log: LogStore,
  branch: BranchId,
):
  | { readonly pinned: Pinned; readonly parent: Parent | undefined }
  | undefined {
  const read = log.read(branch);
  const e = read.ok
    ? knownEvents(read.value).find((x) => x.type === "thread_started")
    : undefined;
  if (e?.type !== "thread_started") return undefined;
  return { pinned: e.data, parent: e.data.parent };
}

function configured(
  log: LogStore,
  artifacts: ArtifactStore,
  row: MemberRow,
):
  | { readonly pinned: Pinned; readonly parent: Parent | undefined }
  | undefined {
  const bytes = artifacts.get(row.config_hash);
  const team = teamRow(log.driver, row.team_id);
  const lead =
    team === undefined ? undefined : log.mainBranch(team.lead_thread_id);
  if (!bytes.ok || team === undefined || lead === undefined || !lead.ok)
    return undefined;
  const pinned = Pinned.parse(
    JSON.parse(new TextDecoder().decode(bytes.value)),
  );
  return {
    pinned,
    parent: { thread_id: team.lead_thread_id, branch_id: lead.value },
  };
}

/**
 * The run budget of the request a member turn belongs to: the root request of its opener's
 * provenance (a receipt's, or its task's), when that request is a user_input with a budget. An
 * operator request's run budget only attributes in Phase 1.
 */
export function runCovering(
  log: LogStore,
): (opener: KnownEvent) => Covering | undefined {
  const found = new Map<string, Covering | undefined>();
  return (opener) => {
    const root = rootOf(log, opener);
    if (root === undefined) return undefined;
    const key = `run:${root.thread_id}:${root.event_id}`;
    if (!found.has(key)) found.set(key, budgetOf(log, root, key));
    return found.get(key);
  };
}

function rootOf(
  log: LogStore,
  opener: KnownEvent,
): { readonly thread_id: string; readonly event_id: string } | undefined {
  if (opener.type === "message_received")
    return opener.data.envelope.provenance.root_request;
  if (opener.type !== "user_input" || opener.data.mail_id === undefined)
    return undefined;
  return mailEnvelope(log.driver, opener.data.mail_id)?.provenance.root_request;
}

function budgetOf(
  log: LogStore,
  root: { readonly thread_id: string; readonly event_id: string },
  budgetId: string,
): Covering | undefined {
  const branch = log.mainBranch(ThreadId.parse(root.thread_id));
  const read = branch.ok ? log.read(branch.value) : undefined;
  if (read === undefined || !read.ok) return undefined;
  const input = knownEvents(read.value).find(
    (e) => e.event_id === root.event_id,
  );
  const budget = input?.type === "user_input" ? input.data.budget : undefined;
  return budget === undefined ? undefined : { budgetId, budget, scope: "run" };
}
