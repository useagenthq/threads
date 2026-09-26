import { z } from "zod";
import {
  BranchId,
  Int,
  type KnownEvent,
  type MemberRef,
  type Principal,
  RequestId,
  type TeamId,
} from "../../log";
import { knownEvents } from "../../reduce";
import type { LogStore } from "../../store";
import { reading } from "../../store/driver";
import { memberRows, refOf, type TeamRow, teamRow } from "../../team/rows";
import type { TeamSource } from "./handle-types";

// Where each feed row's event was written (spec/schema/README.md, "The feed"): the member whose
// branch it is, the operator request a team-log event belongs to, or the team log itself. Every
// branch is read once, and a member materialized since the reader looked is read again, once.

/** One team_feed row, parsed: storage is a boundary. */
export const FeedRow: z.ZodObject<{
  epoch: typeof Int;
  feed_offset: typeof Int;
  branch_id: typeof BranchId;
  seq: typeof Int;
}> = z.strictObject({
  epoch: Int,
  feed_offset: Int,
  branch_id: BranchId,
  seq: Int,
});
export type FeedRow = z.infer<typeof FeedRow>;

/** Each feed row's event and where it was written, reading every branch once. */
export class Sources {
  readonly #log: LogStore;
  /** Each branch read, its events by seq. */
  readonly #branches = new Map<string, ReadonlyMap<number, KnownEvent>>();
  /** The team log's events, by event id, attributed to their operator request. */
  readonly #requests = new Map<string, string>();
  readonly #principals = new Map<string, Principal>();
  readonly #row: TeamRow;
  #members: ReadonlyMap<string, MemberRef>;

  private constructor(
    log: LogStore,
    row: TeamRow,
    members: ReadonlyMap<string, MemberRef>,
  ) {
    this.#log = log;
    this.#row = row;
    this.#members = members;
  }

  static async open(log: LogStore, team: TeamId): Promise<Sources> {
    const row = await reading(log.driver, (tx) => teamRow(tx, team));
    if (row === undefined) throw new Error(`no team ${team}`);
    return new Sources(log, row, await membersOf(log, row));
  }

  async at(row: FeedRow): Promise<{
    readonly event: KnownEvent;
    readonly source: TeamSource;
  }> {
    let event = (await this.#events(row.branch_id)).get(row.seq);
    if (event === undefined) {
      // A follower's branch grew since it was read: read it again, once.
      this.#branches.delete(row.branch_id);
      event = (await this.#events(row.branch_id)).get(row.seq);
    }
    if (event === undefined)
      throw new Error(`the feed names ${row.branch_id}@${row.seq}, not stored`);
    if (row.branch_id === this.#row.team_log_branch_id)
      return { event, source: this.#operator(event) };
    return {
      event,
      source: { kind: "member", member: await this.#member(row) },
    };
  }

  /** The member whose branch it is; members materialized since are read again, once. */
  async #member(row: FeedRow): Promise<MemberRef> {
    const known = this.#members.get(row.branch_id);
    if (known !== undefined) return known;
    this.#members = await membersOf(this.#log, this.#row);
    const member = this.#members.get(row.branch_id);
    if (member === undefined)
      throw new Error(`the feed names ${row.branch_id}, no member's`);
    return member;
  }

  async #events(branch: BranchId): Promise<ReadonlyMap<number, KnownEvent>> {
    const known = this.#branches.get(branch);
    if (known !== undefined) return known;
    const read = await this.#log.read(branch);
    if (!read.ok) throw new Error(`team log ${branch}: ${read.error.message}`);
    const events = knownEvents(read.value);
    const bySeq = new Map(events.map((e) => [e.seq, e]));
    this.#branches.set(branch, bySeq);
    // Attribution reads earlier team-log events: fold them all once, in order.
    for (const e of events) {
      const rid = this.#requestOf(e);
      if (rid !== undefined) this.#requests.set(e.event_id, rid);
      if (e.type === "operator_request")
        this.#principals.set(e.data.request_id, e.data.principal);
    }
    return bySeq;
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
        return this.#requests.get(
          e.data.provenance?.root_request.event_id ?? "",
        );
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

/** Each member branch of the team, as the ref its feed items name. */
async function membersOf(
  log: LogStore,
  team: TeamRow,
): Promise<ReadonlyMap<string, MemberRef>> {
  return new Map(
    (await reading(log.driver, (tx) => memberRows(tx, team.team_id))).flatMap(
      (r) =>
        r.branch_id === null ? [] : [[r.branch_id, refOf(team, r)] as const],
    ),
  );
}
