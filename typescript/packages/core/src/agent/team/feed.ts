import { z } from "zod";
import {
  BranchId,
  Int,
  type KnownEvent,
  type Principal,
  RequestId,
  type TeamId,
} from "../../log";
import { knownEvents } from "../../reduce";
import type { LogStore } from "../../store";
import { memberRows, refOf, teamRow } from "../../team/rows";
import type { TeamCursor, TeamItem, TeamSource } from "./handle-types";

// team.events() without follow (spec/schema/README.md, "The feed"): a pure read of the team_feed
// rows of the current epoch in offset order, each event as stored with its cursor and source. It
// never writes and never drives the team.

const FeedRow = z.strictObject({
  epoch: Int,
  feed_offset: Int,
  branch_id: BranchId,
  seq: Int,
});
type FeedRow = z.infer<typeof FeedRow>;

/** The committed feed after `after`, then the end. */
export async function* teamEvents(
  log: LogStore,
  team: TeamId,
  after: TeamCursor | undefined,
): AsyncGenerator<TeamItem> {
  const rows = z.array(FeedRow).parse(
    log.driver.all(
      `SELECT epoch, feed_offset, branch_id, seq FROM team_feed
        WHERE team_id = ? AND epoch = (SELECT MAX(epoch) FROM team_feed WHERE team_id = ?)
        ORDER BY feed_offset`,
      [team, team],
    ),
  );
  const epoch = rows[0]?.epoch;
  if (epoch === undefined) return;
  const restarted = after !== undefined && after.epoch !== epoch;
  if (restarted)
    yield { kind: "epoch_restarted", cursor: { epoch, offset: 0 } };
  const from = after === undefined || restarted ? 0 : after.offset;
  const sources = new Sources(log, team);
  for (const row of rows.filter((r) => r.feed_offset > from)) {
    const { event, source } = sources.at(row);
    yield {
      kind: "event",
      cursor: { epoch, offset: row.feed_offset },
      source,
      event,
    };
  }
}

/** Each feed row's event and where it was written, reading every branch once. */
class Sources {
  readonly #log: LogStore;
  readonly #team: TeamId;
  readonly #branches = new Map<string, readonly KnownEvent[]>();
  /** The team log's events, by event id, attributed to their operator request. */
  readonly #requests = new Map<string, string>();
  readonly #principals = new Map<string, Principal>();

  constructor(log: LogStore, team: TeamId) {
    this.#log = log;
    this.#team = team;
  }

  at(row: FeedRow): {
    readonly event: KnownEvent;
    readonly source: TeamSource;
  } {
    const events = this.#events(row.branch_id);
    const event = events.find((e) => e.seq === row.seq);
    if (event === undefined)
      throw new Error(`the feed names ${row.branch_id}@${row.seq}, not stored`);
    const db = this.#log.driver;
    const team = teamRow(db, this.#team);
    if (team === undefined) throw new Error(`no team ${this.#team}`);
    if (row.branch_id === team.team_log_branch_id)
      return { event, source: this.#operator(event) };
    const member = memberRows(db, this.#team).find(
      (r) => r.branch_id === row.branch_id,
    );
    if (member === undefined)
      throw new Error(`the feed names ${row.branch_id}, no member's`);
    return { event, source: { kind: "member", member: refOf(team, member) } };
  }

  #events(branch: BranchId): readonly KnownEvent[] {
    const known = this.#branches.get(branch);
    if (known !== undefined) return known;
    const read = this.#log.read(branch);
    if (!read.ok) throw new Error(`team log ${branch}: ${read.error.message}`);
    const events = knownEvents(read.value);
    this.#branches.set(branch, events);
    // Attribution reads earlier team-log events: fold them all once, in order.
    for (const e of events) {
      const rid = this.#requestOf(e);
      if (rid !== undefined) this.#requests.set(e.event_id, rid);
      if (e.type === "operator_request")
        this.#principals.set(e.data.request_id, e.data.principal);
    }
    return events;
  }

  #operator(e: KnownEvent): TeamSource {
    const rid = this.#requests.get(e.event_id);
    const principal = rid === undefined ? undefined : this.#principals.get(rid);
    return rid === undefined || principal === undefined
      ? { kind: "team" }
      : { kind: "operator", principal, request: RequestId.parse(rid) };
  }

  /** The operator request a team-log event belongs to (spec/schema/README.md, "The feed"). */
  #requestOf(e: KnownEvent): string | undefined {
    switch (e.type) {
      case "operator_request":
      case "operator_refused":
        return e.data.request_id;
      case "message_policy_decided":
        return e.data.request_id;
      case "member_started":
        return this.#requests.get(e.data.provenance.root_request.event_id);
      case "message_sent":
        return "operator" in e.data.envelope.from
          ? e.data.envelope.from.operator
          : undefined;
      case "message_received": {
        const env = e.data.envelope;
        if (env.ask_id !== undefined) return keyed(env.ask_id);
        if (env.monitor_id !== undefined)
          return this.#registered(env.monitor_id);
        return this.#requests.get(env.provenance.root_request.event_id);
      }
      case "member_observed":
        return this.#registered(e.data.monitor_id);
      case "ask_closed":
        return keyed(e.data.ask_id);
      case "wait_started":
      case "wait_finished":
        return keyed(e.data.wait_id);
      default:
        return undefined;
    }
  }

  /** A monitor's request: that of its registering event (`<branch>:<event_id>:<target>`). */
  #registered(monitor: string): string | undefined {
    const [, event] = monitor.split(":");
    return event === undefined ? undefined : this.#requests.get(event);
  }
}

/** An ask's or wait's request: its id is `<team log branch>:<request_id>`. */
const keyed = (id: string): string | undefined => id.split(":")[1];
