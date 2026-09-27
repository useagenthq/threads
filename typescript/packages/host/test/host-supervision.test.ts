import { afterEach, describe, expect, test } from "bun:test";
import { agent, type Store, sqlite } from "@threads/core";
import {
  hostTeamIds,
  type KnownEvent,
  storeConnection,
} from "@threads/core/host";
import { z } from "zod";
import { MemberName } from "../../core/src/log";
import { answering, answers, replyTo } from "../../core/test/team/run-kit";
import type { HostOptions } from "../src/host";
import {
  alice,
  type Harness,
  harness,
  knownEventsOf,
  say,
  until,
  use,
} from "./kit";
import { sqlAll } from "./sql";
import { assertReplays } from "./team-kit";

// Supervision of a host member (lane 29E, section E): the host team log's writer decides once on
// each ended generation, a restart opens the next one in a new empty thread on the pin this host
// holds now, and a stop is final until an operator starts it.
//
// A host member ends failed only on a rebind failure, and the way to cause one here is the real
// one: the definition behind a name changes while its row still names the pin it started on, so
// the next run of that generation cannot rebind it. Mirrors Python's
// tests/host/test_host_supervision.py.

const IDS = hostTeamIds(alice.tenant);

const live: Harness[] = [];
afterEach(async () => {
  for (const h of live.splice(0)) await h.host.stop();
});

/** billing as one host defines it; `note` changes its pin without changing its behaviour. */
const billingSaying = (note: string) =>
  agent({
    name: "billing",
    instructions: `You are billing. ${note}`,
    model: answers([
      (request) => replyTo("r1", request, "Paid."),
      () => say("Answered."),
    ]),
  });

const ASK = use("ask", { to: "billing", question: "Is INV-1001 paid?" }, "c1");

/**
 * A caller that asks billing once per turn and then answers its user. Its answer is made from
 * the request it is given rather than from a position in a script, so two runs of one host never
 * race each other for the next scripted response.
 */
const supportAsking = () =>
  agent({
    name: "support",
    model: answering((request) =>
      request.includes("c1") ? say("Billing answered.") : ASK,
    ),
  });

type Members = NonNullable<HostOptions["members"]>;

function hosted(store: Store, note: string, members: Members): Harness {
  const h = harness({
    store,
    agents: { billing: billingSaying(note), support: supportAsking() },
    members,
    messagePolicy: [{ from: "support", to: "billing", allow: ["ask"] }],
  });
  live.push(h);
  return h;
}

const MemberRow = z.object({
  name: z.string(),
  generation: z.number(),
  state: z.string(),
  thread_id: z.string(),
  config_hash: z.string(),
  branch_id: z.string().nullable(),
});

async function members(
  store: Store,
): Promise<readonly z.infer<typeof MemberRow>[]> {
  const { db } = await storeConnection(store);
  return z.array(MemberRow).parse(
    await sqlAll(
      db,
      `SELECT name, generation, state, thread_id, config_hash, branch_id
           FROM team_members WHERE team_id = ? ORDER BY generation`,
      [IDS.teamId],
    ),
  );
}

const teamLog = (store: Store): Promise<readonly KnownEvent[]> =>
  knownEventsOf(store, alice.tenant, IDS.branchId);

async function decisions(store: Store): Promise<readonly EventOfDecided[]> {
  return (await teamLog(store)).flatMap((e) =>
    e.type === "supervisor_decided" ? [e] : [],
  );
}
type EventOfDecided = Extract<KnownEvent, { type: "supervisor_decided" }>;

/** Every mail row's state, and every stale_member refusal anywhere: the negative reads both. */
async function staleness(
  store: Store,
): Promise<{ readonly states: readonly string[]; readonly refusals: number }> {
  const { db } = await storeConnection(store);
  const states = z
    .array(z.object({ state: z.string() }))
    .parse(
      await sqlAll(db, "SELECT state FROM mail WHERE team_id = ?", [
        IDS.teamId,
      ]),
    )
    .map((r) => r.state);
  const branches = z
    .array(z.object({ branch_id: z.string() }))
    .parse(
      await sqlAll(db, "SELECT branch_id FROM branches WHERE tenant_id = ?", [
        alice.tenant,
      ]),
    );
  let refusals = 0;
  for (const { branch_id } of branches)
    for (const e of await knownEventsOf(store, alice.tenant, branch_id))
      if (e.type === "mail_refused" && e.data.code === "stale_member")
        refusals += 1;
  return { states, refusals };
}

/** Mail rows of this host team that no writer has taken yet. */
async function pending(store: Store): Promise<number> {
  const { db } = await storeConnection(store);
  return z
    .array(z.object({ state: z.string() }))
    .parse(
      await sqlAll(
        db,
        "SELECT state FROM mail WHERE team_id = ? AND state = 'pending'",
        [IDS.teamId],
      ),
    ).length;
}

/** support's run, which asks billing once. Its own answer is not what these tests read. */
async function asks(h: Harness, key: string): Promise<void> {
  const run = await h.host.startRun(
    { agent: "support", input: "Is INV-1001 paid?" },
    { principal: alice, idempotencyKey: key },
  );
  if (!run.ok) throw new Error(run.error.message);
}

/**
 * A store whose host team is open with billing at generation 1, idle, pinned on the definition
 * one host had — and a second, live host that defines billing differently. The next run of
 * generation 1 rebinds against that second definition and fails, which is the only way a host
 * member ends failed.
 */
async function drifted(members_: Members): Promise<Harness> {
  const store = sqlite(":memory:");
  const first = hosted(store, "v1", members_);
  await first.host.ready();
  // The tenant's host team opens on its first run, so one caller run of alice's brings
  // generation 1 into being and leaves it idle, pinned on this host's definition.
  await asks(first, "k-0");
  // Settled, not merely materialized: generation 1 has taken that ask and answered it, so the
  // failure the next host sees is its rebind and nothing left over from this one.
  await until(async () => {
    const row = (await members(store))[0];
    return row?.state === "idle" && (await pending(store)) === 0;
  }, 20_000);
  await first.host.stop();
  live.splice(live.indexOf(first), 1);
  const second = hosted(store, "v2", members_);
  await second.host.ready();
  return second;
}

const decided = (store: Store) => async (): Promise<boolean> =>
  (await decisions(store)).length > 0;

describe("the supervisor of a host member", () => {
  test("a failed rebind restarts it into a new empty thread, on the pin the host holds now", async () => {
    const h = await drifted({ billing: {} });
    const [first] = await members(h.store);
    if (first === undefined) throw new Error("generation 1 has a row");
    await asks(h, "k-1");
    await until(decided(h.store), 20_000);
    await until(async () => (await members(h.store)).length === 2, 20_000);

    const one = (await decisions(h.store))[0];
    if (one === undefined) throw new Error("one decision");
    expect(await decisions(h.store)).toHaveLength(1);
    expect(one.data.action).toBe("restart");
    expect(one.data.restarts_in_window).toBe(0);
    expect(one.data.member.generation).toBe(1);
    // Its `ended` is generation 1's own member_ended, by position in that log.
    expect(String(one.data.ended.branch_id)).toBe(first.branch_id ?? "");

    const restarted = (await teamLog(h.store)).findLast(
      (e) => e.type === "member_started",
    );
    if (restarted?.type !== "member_started") throw new Error("a restart");
    expect(restarted.data.restart_of).toBe(1);
    expect(restarted.data.member.generation).toBe(2);
    // Re-pinned: the new generation runs the definition this host has, not the one that failed.
    expect(restarted.data.config_hash).not.toBe(first.config_hash);

    const rows = await members(h.store);
    expect(rows.map((r) => [r.generation, r.state])).toEqual([
      [1, "ended"],
      [2, "idle"],
    ]);
    // Decision 6's completeness, which no conformance case pins: the end append is the last
    // word of that generation. After member_ended come only the refusals and bounces it owes,
    // and nothing terminates the member a second time.
    const own1 = await knownEventsOf(
      h.store,
      alice.tenant,
      first.branch_id ?? "",
    );
    const after = own1.slice(
      own1.findIndex((e) => e.type === "member_ended") + 1,
    );
    expect(new Set(after.map((e) => e.type))).toEqual(
      new Set(["mail_refused", "message_sent"]),
    );
    const next = rows[1];
    if (next === undefined) throw new Error("generation 2 has a row");
    expect(next.thread_id).not.toBe(first.thread_id);
    // Erlang-style: a restarted member has fresh state, so its log is its own opening alone.
    const own = await knownEventsOf(
      h.store,
      alice.tenant,
      next.branch_id ?? "",
    );
    expect(own.map((e) => e.type)).toEqual(["thread_started"]);
    await assertReplays(h.store, alice.tenant, IDS.teamId);
  }, 40_000);

  test("restart never records a stop, and the name stays ended", async () => {
    const h = await drifted({ billing: { restart: "never" } });
    await asks(h, "k-1");
    await until(decided(h.store), 20_000);
    const all = await decisions(h.store);
    expect(all).toHaveLength(1);
    expect(all[0]?.data.action).toBe("stop");
    expect(all[0]?.data.policy.restart).toBe("never");
    // No second generation: every end is on record, and this one stops the name.
    expect((await members(h.store)).map((r) => r.generation)).toEqual([1]);
    await assertReplays(h.store, alice.tenant, IDS.teamId);
  }, 40_000);

  test("two hosts on one store racing an end decide it once", async () => {
    const h = await drifted({ billing: {} });
    const other = hosted(h.store, "v2", { billing: {} });
    await other.host.ready();
    await asks(h, "k-1");
    await until(decided(h.store), 20_000);
    await until(async () => (await members(h.store)).length === 2, 20_000);
    // Both hosts see the same ended generation; the team log's writer decides it once, and the
    // loser's own append is refused by rule 51 and rolls back.
    expect(await decisions(h.store)).toHaveLength(1);
    expect(
      (await teamLog(h.store)).filter((e) => e.type === "member_started"),
    ).toHaveLength(2);
    await assertReplays(h.store, alice.tenant, IDS.teamId);
  }, 40_000);
});

describe("stale generations are unreachable (M29-1)", () => {
  test("an ask across a restart is returned, never stale, and the next one is answered", async () => {
    const h = await drifted({ billing: {} });
    await asks(h, "k-1");
    await until(decided(h.store), 20_000);
    await until(async () => (await members(h.store)).length === 2, 20_000);

    // The end refused the pending ask itself, so its row is returned, not stale, and no sender
    // could ever find a row addressed to a generation that is gone.
    const after = await staleness(h.store);
    expect(after.refusals).toBe(0);
    expect(after.states.filter((s) => s === "stale")).toEqual([]);
    expect(after.states).toContain("returned");

    // A second caller binds the current generation in its own transaction: generation 2 answers.
    await asks(h, "k-2");
    await until(async () => {
      const rows = await members(h.store);
      const branch = rows[1]?.branch_id ?? "";
      const own = await knownEventsOf(h.store, alice.tenant, branch);
      return own.some((e) => e.type === "message_sent");
    }, 20_000);
    const end = await staleness(h.store);
    expect(end.refusals).toBe(0);
    expect(end.states.filter((s) => s === "stale")).toEqual([]);
    await assertReplays(h.store, alice.tenant, IDS.teamId);
  }, 40_000);
});

/** The tenant's host team as an operator holds it. */
async function operatorTeam(h: Harness) {
  const got = await h.host.team({ principal: alice });
  if (!got.ok) throw new Error(got.error.message);
  return got.value;
}

const ref = (generation: number) => ({
  tenant: alice.tenant,
  team: IDS.teamId,
  name: MemberName.parse("billing"),
  generation,
});

describe("an operator's stop and restart", () => {
  test("a cancel ends the member cancelled, and the supervisor stops rather than restarts", async () => {
    const store = sqlite(":memory:");
    const h = hosted(store, "v1", { billing: {} });
    await h.host.ready();
    await asks(h, "k-0");
    await until(async () => {
      const row = (await members(store))[0];
      return row?.state === "idle" && (await pending(store)) === 0;
    }, 20_000);

    const team = await operatorTeam(h);
    expect(await team.cancel(ref(1))).toEqual({
      status: "cancel_requested",
      member: ref(1),
    });
    await until(decided(store), 20_000);
    const all = await decisions(store);
    expect(all).toHaveLength(1);
    // A cancel is a stop: it is what an operator asked for, so it is never undone by a restart.
    expect(all[0]?.data.action).toBe("stop");
    expect((await members(store)).map((r) => r.generation)).toEqual([1]);
    await assertReplays(store, alice.tenant, IDS.teamId);
  }, 40_000);

  test("after a stop, mail is refused member_ended and team.start opens the next generation", async () => {
    const h = await drifted({ billing: { restart: "never" } });
    await asks(h, "k-1");
    await until(decided(h.store), 20_000);
    const team = await operatorTeam(h);
    // The row is ended, so a send to the name is refused where it is sent, not where it lands.
    expect(await team.send(ref(1), "Are you there?")).toEqual({
      status: "refused",
      code: "member_ended",
    });

    const started = await team.start("billing");
    expect(started).toEqual({ status: "started", member: ref(2) });
    const log = await teamLog(h.store);
    const restarted = log.findLast((e) => e.type === "member_started");
    if (restarted?.type !== "member_started") throw new Error("a restart");
    expect(restarted.data.restart_of).toBe(1);
    // An operator's restart names the operator_request that asked for it (rule 51).
    expect(restarted.data.provenance?.root_request.thread_id).toBe(
      IDS.threadId,
    );
    await until(
      async () => (await members(h.store))[1]?.state === "idle",
      20_000,
    );
    expect(await team.send(ref(2), "Are you there?")).toMatchObject({
      status: "sent",
    });
    await assertReplays(h.store, alice.tenant, IDS.teamId);
  }, 40_000);

  test("a name that is no host member, and a live generation, are both forbidden", async () => {
    const store = sqlite(":memory:");
    const h = hosted(store, "v1", { billing: {} });
    await h.host.ready();
    await asks(h, "k-0");
    await until(async () => (await members(store)).length === 1, 20_000);
    const team = await operatorTeam(h);
    expect(await team.start("payroll")).toEqual({
      status: "refused",
      code: "forbidden",
    });
    // Nothing to restart while the current generation is live.
    expect(await team.start("billing")).toEqual({
      status: "refused",
      code: "forbidden",
    });
    await assertReplays(store, alice.tenant, IDS.teamId);
  }, 40_000);
});
