import type { EventOf } from "../../fold/state";
import { type KnownEvent, ThreadId } from "../../log";
import type { Covering } from "../../loop";
import { knownEvents } from "../../reduce";
import type { LogStore } from "../../store";
import { mailEnvelope } from "../../team/rows";

// A member's budgets (spec/schema/README.md, "Teams"; design §2.6): before every model request of
// a member turn the ledger reserves against the member's own thread budget, every ancestor
// thread's budget through its structural parents (team_member, subagent, handoff) up to the root,
// and the run budget of that turn's root request.

type Parent = NonNullable<EventOf<"thread_started">["data"]["parent"]>;

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
