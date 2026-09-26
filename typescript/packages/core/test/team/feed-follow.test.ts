import { describe, expect, test } from "bun:test";
import {
  agent,
  InvalidCursorError,
  scriptedModel,
  sqlite,
  type TeamCursor,
  type TeamItem,
} from "../../src";
import { teamEvents } from "../../src/agent/team/feed";
import type { TeamId } from "../../src/log";
import type { LogStore } from "../../src/store";
import { rebuildTeamIndex } from "../../src/team/rebuild";
import { unwrap } from "../store/helpers";
import { assertTeamReplays } from "./kit";
import { logOf, say, start } from "./run-kit";

// team.events({follow}) (lane 29B): the committed feed, then new items as they commit. The
// in-process wake means a follower with a very long poll still sees an append at once. Every
// test ends with the team's replay check.

/** Long enough that only the in-process wake can move these tests along. */
const NO_POLL = 60_000;

function lead(responses: readonly string[]) {
  return agent({
    name: "lead",
    model: scriptedModel({ responses: responses.map((t) => say(t)) }),
    team: [
      agent({
        name: "writer",
        model: scriptedModel({ responses: [say("Draft.")] }),
      }),
    ],
  });
}

async function ran(responses: readonly string[] = ["Ready.", "Noted."]) {
  const store = sqlite(":memory:");
  const r = await lead(responses).run("Get ready.", { store });
  return { store, r, team: r.team, log: await logOf(store) };
}

const idOf = (item: TeamItem): string =>
  `${item.cursor.epoch}:${item.cursor.offset}`;

/** The first `n` items of a follower, then it stops iterating (the reader is killed). */
async function take(
  items: AsyncIterable<TeamItem>,
  n: number,
): Promise<TeamItem[]> {
  const seen: TeamItem[] = [];
  for await (const item of items) {
    seen.push(item);
    if (seen.length === n) break;
  }
  return seen;
}

describe("team.events({follow})", () => {
  test("new items reach a live follower, woken by the append that committed them", async () => {
    const { team, log } = await ran();
    const committed = await take(teamEvents(log, team.ref.id), 1_000);
    const seen: TeamItem[] = [];
    const drained = Promise.withResolvers<void>();
    const follower = (async () => {
      for await (const item of teamEvents(log, team.ref.id, {
        follow: true,
        pollMs: NO_POLL,
      })) {
        seen.push(item);
        if (seen.length === committed.length) drained.resolve();
        // The refused start below adds three rows; stop once they have all arrived.
        if (seen.length === committed.length + 3) return;
      }
    })();
    // Only append once the follower has the committed feed, so the rows below reach it through
    // the wake, whose arrival this waits on: its poll is a minute away.
    await drained.promise;
    // A start of an agent the team does not list: refused, and three team-log events.
    expect((await team.start("editor", "Go.")).status).toBe("refused");
    await follower;
    expect(seen.slice(0, committed.length)).toEqual(committed);
    expect(seen.map(idOf)).toEqual([...new Set(seen.map(idOf))]);
    expect(await teamAll(log, team.ref.id)).toEqual(seen);
    await assertTeamReplays(log, team.ref.id);
  });

  test("a follower killed mid-stream resumes from its last id with no gap and no duplicate", async () => {
    const { team, log } = await ran();
    const whole = await teamAll(log, team.ref.id);
    expect(whole.length).toBeGreaterThan(4);
    // Killed after three items: everything it saw, and nothing more.
    const before = await take(
      teamEvents(log, team.ref.id, { follow: true, pollMs: NO_POLL }),
      3,
    );
    const last = before.at(-1)?.cursor;
    if (last === undefined) throw new Error("three items");
    const resumed = await take(
      teamEvents(log, team.ref.id, {
        after: last,
        follow: true,
        pollMs: NO_POLL,
      }),
      whole.length - 3,
    );
    expect([...before, ...resumed]).toEqual(whole);
    expect(new Set([...before, ...resumed].map(idOf)).size).toBe(whole.length);
    await assertTeamReplays(log, team.ref.id);
  });

  test("an index wipe mid-follow yields one epoch_restarted, then the new epoch", async () => {
    const { team, log } = await ran();
    const whole = await teamAll(log, team.ref.id);
    const items = teamEvents(log, team.ref.id, {
      follow: true,
      pollMs: NO_POLL,
    });
    const seen: TeamItem[] = [];
    for await (const item of items) {
      seen.push(item);
      if (seen.length === 2) unwrap(await rebuildTeamIndex(log, team.ref.id));
      // The wiped epoch's items, then the restart, then the whole of epoch 2.
      if (seen.length === whole.length + 1 + whole.length) break;
    }
    const restart = seen.find((i) => i.kind === "epoch_restarted");
    if (restart === undefined) throw new Error("one epoch_restarted");
    expect(seen.filter((i) => i.kind === "epoch_restarted")).toEqual([restart]);
    expect(restart).toEqual({
      kind: "epoch_restarted",
      cursor: { epoch: 2, offset: 0 },
    });
    const after = seen.slice(seen.indexOf(restart) + 1);
    expect(after.map((i) => i.cursor.epoch)).toEqual(after.map(() => 2));
    expect(after).toHaveLength(whole.length);
    await assertTeamReplays(log, team.ref.id);
  });

  test("a follower ends when the team closes, after the closing append's items", async () => {
    // The lead's script runs out on its wake turn: the run fails, and the lead's member_ended
    // closes its team in the same append.
    const store = sqlite(":memory:");
    const closing = agent({
      name: "lead",
      model: scriptedModel({
        responses: [start("c1", "writer", "Go."), say("Started.")],
      }),
      team: [
        agent({
          name: "writer",
          model: scriptedModel({ responses: [say("Draft.")] }),
        }),
      ],
    });
    const r = await closing.run("Work.", { store });
    expect(r.status).toBe("failed");
    const log = await logOf(store);
    const whole = await teamAll(log, r.team.ref.id);
    const seen = await take(
      teamEvents(log, r.team.ref.id, { follow: true, pollMs: NO_POLL }),
      whole.length + 1,
    );
    // It ended by itself, without the take() bound: a closed team ends its followers.
    expect(seen).toEqual(whole);
    await assertTeamReplays(log, r.team.ref.id);
  });
});

describe("team.events cursors", () => {
  test("a cursor of a later epoch, or past the head, is invalid_cursor", async () => {
    const { team, log } = await ran();
    const whole = await teamAll(log, team.ref.id);
    const head = whole.at(-1)?.cursor;
    if (head === undefined) throw new Error("a feed");
    // At the head is a position, not an error: it yields nothing and ends.
    expect(await teamAll(log, team.ref.id, head)).toEqual([]);
    for (const bad of [
      { epoch: head.epoch + 1, offset: 0 },
      { epoch: head.epoch, offset: head.offset + 1 },
      { epoch: head.epoch, offset: -1 },
    ] satisfies TeamCursor[])
      await expect(teamAll(log, team.ref.id, bad)).rejects.toBeInstanceOf(
        InvalidCursorError,
      );
    await assertTeamReplays(log, team.ref.id);
  });
});

async function teamAll(
  log: LogStore,
  team: TeamId,
  after?: TeamCursor,
): Promise<TeamItem[]> {
  const seen: TeamItem[] = [];
  for await (const item of teamEvents(
    log,
    team,
    after === undefined ? {} : { after },
  ))
    seen.push(item);
  return seen;
}
