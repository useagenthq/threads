import { z } from "zod";
import { Int, type TeamId } from "../../log";
import type { LogStore } from "../../store";
import { reading } from "../../store/driver";
import { TEAM_CONSTANTS } from "../../team/constants";
import { teamChanged } from "../../team/wake";
import { InvalidCursorError } from "../errors";
import { FeedRow, Sources } from "./feed-sources";
import type { TeamCursor, TeamItem } from "./handle-types";

// team.events (spec/schema/README.md, "The feed"; lane 29B adds follow): a pure read of
// team_feed in offset order, each event as stored with its cursor and source. It never writes
// and never drives the team. A follower is woken by an in-process commit at once, and by its
// poll for the commits of other processes.

/** Feed rows read per query: the epoch is paged, never loaded whole. */
const PAGE = 256;

/** The current epoch, its last offset, and whether the team has closed, in one snapshot. */
export type FeedHead = {
  readonly epoch: number;
  readonly head: number;
  readonly closed: boolean;
};

const HeadRow = z.strictObject({
  epoch: Int.nullable(),
  last: Int.nullable(),
  closed_at: Int.nullable(),
});

/**
 * The team's feed extent as one read: `closed_at` and the head come from the same snapshot, so
 * a closed team's head already holds the items of the append that closed it. Undefined when the
 * team has no feed row at all.
 */
export async function feedHead(
  log: LogStore,
  team: TeamId,
): Promise<FeedHead | undefined> {
  const [top] = z.array(HeadRow).parse(
    await reading(log.driver, (tx) =>
      tx.all(
        `SELECT epoch, MAX(feed_offset) AS last,
          (SELECT closed_at FROM teams WHERE team_id = ?) AS closed_at
          FROM team_feed
          WHERE team_id = ? AND epoch = (SELECT MAX(epoch) FROM team_feed WHERE team_id = ?)`,
        [team, team, team],
      ),
    ),
  );
  if (top === undefined || top.epoch === null || top.last === null)
    return undefined;
  return { epoch: top.epoch, head: top.last, closed: top.closed_at !== null };
}

/**
 * How a cursor reads against the feed's head (lane 29B): `restart` for an older epoch, and
 * `invalid_cursor` for a later one or an offset outside the current epoch. Phase 1 restarted on
 * any other epoch; a later epoch is refused now.
 */
export function cursorAgainst(
  head: FeedHead,
  after: TeamCursor,
): "resume" | "restart" | "invalid_cursor" {
  if (after.epoch > head.epoch) return "invalid_cursor";
  if (after.epoch < head.epoch) return "restart";
  return after.offset >= 0 && after.offset <= head.head
    ? "resume"
    : "invalid_cursor";
}

export type TeamEventsOptions = {
  readonly after?: TeamCursor;
  readonly follow?: boolean;
  /** How long a follower waits for another process's commit; tests inject less. */
  readonly pollMs?: number;
};

/**
 * Without `follow`, the feed committed when the call began, after `after`, then the end. With
 * `follow`, the same and then new items as they commit, until the team closes (after the items
 * of the append that closed it) or the caller stops. A rebuilt index is a new epoch: one
 * `epoch_restarted` names it, and the stream goes on from its start.
 */
export async function* teamEvents(
  log: LogStore,
  team: TeamId,
  options: TeamEventsOptions = {},
): AsyncGenerator<TeamItem> {
  const { after, follow = false } = options;
  const pollMs = options.pollMs ?? TEAM_CONSTANTS.wakePollInProcessMs;
  let head = await feedHead(log, team);
  if (head === undefined) return;
  let { epoch, cursor } = startAt(head, after, team);
  const sources = await Sources.open(log, team);
  for (;;) {
    if (head.epoch !== epoch) {
      yield {
        kind: "epoch_restarted",
        cursor: { epoch: head.epoch, offset: 0 },
      };
      epoch = head.epoch;
      cursor = 0;
    }
    for await (const item of drain(log, team, sources, head, cursor)) {
      cursor = item.cursor.offset;
      yield item;
    }
    if (!follow || head.closed) return;
    // The restart above keeps them equal, so the wait compares against this epoch's head.
    head = await nextHead(log, team, head, cursor, pollMs);
  }
}

/**
 * Where the stream opens: the offset to resume from, and the epoch it believes it is in. An
 * older `after` leaves that epoch behind the head, so the loop opens with one `epoch_restarted`.
 */
function startAt(
  head: FeedHead,
  after: TeamCursor | undefined,
  team: TeamId,
): { epoch: number; cursor: number } {
  if (after === undefined) return { epoch: head.epoch, cursor: 0 };
  const against = cursorAgainst(head, after);
  if (against === "invalid_cursor")
    throw new InvalidCursorError(
      `cursor ${after.epoch}:${after.offset} is not in team ${team}'s feed`,
    );
  return against === "resume"
    ? { epoch: head.epoch, cursor: after.offset }
    : { epoch: after.epoch, cursor: 0 };
}

/** The head a follower goes on from: at once when the feed has already moved, else after a wait. */
async function nextHead(
  log: LogStore,
  team: TeamId,
  head: FeedHead,
  cursor: number,
  pollMs: number,
): Promise<FeedHead> {
  // Registered before the look, so a commit between the look and the wait still wakes it.
  const wake = teamChanged(team);
  const next = await feedHead(log, team);
  if (next !== undefined && (next.epoch !== head.epoch || next.head > cursor)) {
    wake.done();
    return next;
  }
  await waitFor(wake, pollMs);
  return (await feedHead(log, team)) ?? head;
}

/** The rows after `cursor` up to the snapshot's head, paged, as items. */
async function* drain(
  log: LogStore,
  team: TeamId,
  sources: Sources,
  head: FeedHead,
  cursor: number,
): AsyncGenerator<Extract<TeamItem, { kind: "event" }>> {
  let from = cursor;
  while (from < head.head) {
    const rows = await page(log, team, head.epoch, from, head.head);
    for (const row of rows) {
      const { event, source } = await sources.at(row);
      yield {
        kind: "event",
        cursor: { epoch: head.epoch, offset: row.feed_offset },
        source,
        event,
      };
      from = row.feed_offset;
    }
    if (rows.length < PAGE) return;
  }
}

async function page(
  log: LogStore,
  team: TeamId,
  epoch: number,
  from: number,
  end: number,
): Promise<readonly FeedRow[]> {
  return z.array(FeedRow).parse(
    await reading(log.driver, (tx) =>
      tx.all(
        `SELECT epoch, feed_offset, branch_id, seq FROM team_feed
        WHERE team_id = ? AND epoch = ? AND feed_offset > ? AND feed_offset <= ?
        ORDER BY feed_offset LIMIT ?`,
        [team, epoch, from, end, PAGE],
      ),
    ),
  );
}

/** The next in-process commit, or the poll, whichever comes first. */
async function waitFor(
  wake: ReturnType<typeof teamChanged>,
  pollMs: number,
): Promise<void> {
  const poll = Promise.withResolvers<void>();
  const timer = setTimeout(poll.resolve, pollMs);
  try {
    await Promise.race([wake.woken, poll.promise]);
  } finally {
    clearTimeout(timer);
    wake.done();
  }
}
