import { describe, expect, test } from "bun:test";
import {
  agent,
  type MemberRef,
  type Model,
  openTeam,
  scriptedModel,
  sqlite,
  type Team,
} from "../../src";
import { AskId, MemberName } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { reading } from "../../src/store/driver";
import { teamRow } from "../../src/team/rows";
import { unwrap } from "../store/helpers";
import { assertTeamReplays } from "./kit";
import { answers, logOf, replyTo, say, types } from "./run-kit";

// The operator's ask, wait, cancel and askStatus (spec/api.json Team; spec/schema/README.md,
// "Teams", the operator's handle): each writing method is one operator request in the team log;
// ask and wait return the outcome the team log records, driving the team until it does.

const refIn = (team: Team, name: string): MemberRef => ({
  tenant: team.ref.tenant,
  team: team.ref.id,
  name: MemberName.parse(name),
  generation: 1,
});

/**
 * A lead that ran once and listed `writer` (and `reader`), and its team's handle. A lead name of
 * its own keeps openTeam from binding another test's lead of the same config.
 */
async function ran(writer: Model, reader?: Model, name = "lead") {
  const store = sqlite(":memory:");
  const lead = agent({
    name,
    model: scriptedModel({ responses: [say("Ready.")] }),
    team: [
      agent({ name: "writer", model: writer }),
      ...(reader === undefined
        ? []
        : [agent({ name: "reader", model: reader })]),
    ],
  });
  const r = await lead.run("Get ready.", { store });
  return { store, team: r.team, lead };
}

async function teamLog(store: ReturnType<typeof sqlite>, team: Team) {
  const log = await logOf(store);
  const row = await reading(log.driver, (tx) => teamRow(tx, team.ref.id));
  if (row === undefined) throw new Error("no team");
  return knownEvents(unwrap(await log.read(row.team_log_branch_id)));
}

describe("team.ask", () => {
  test("the member's reply is the ask's outcome; askStatus reads the same", async () => {
    const { store, team } = await ran(
      answers([
        () => say("Drafted."),
        (request) => replyTo("r1", request, "Batteries."),
        () => say("Replied."),
      ]),
    );
    await team.start("writer", "Draft the summary.");
    const writer = refIn(team, "writer-1");
    const got = await team.ask(writer, "Which topic?");
    expect(got).toMatchObject({
      status: "answered",
      text: "Batteries.",
      member: writer,
    });
    if (got.status !== "answered") throw new Error("answered");
    expect(await team.askStatus(got.askId)).toEqual(got);
    expect(types(await teamLog(store, team))).toContain("ask_closed");
    await assertTeamReplays(await logOf(store), team.ref.id);
  });

  test("no reply by the deadline is timed_out; a retry under its key re-attaches and records nothing", async () => {
    const { store, team } = await ran(
      answers([() => say("Drafted."), () => say("No idea.")]),
    );
    await team.start("writer", "Draft.");
    const writer = refIn(team, "writer-1");
    const options = { timeoutMs: 300, idempotencyKey: "ask-1" };
    const first = await team.ask(writer, "Which topic?", options);
    expect(first.status).toBe("timed_out");
    const before = (await teamLog(store, team)).length;
    expect(await team.ask(writer, "Which topic?", options)).toEqual(first);
    expect(await teamLog(store, team)).toHaveLength(before);
    await assertTeamReplays(await logOf(store), team.ref.id);
  });

  test("an ask to the lead is refused lead, and waiting for it still works", async () => {
    const { store, team } = await ran(answers([]));
    const lead = refIn(team, "lead");
    expect(await team.ask(lead, "Which topic?")).toEqual({
      status: "refused",
      code: "lead",
    });
    expect(types(await teamLog(store, team)).slice(-3)).toEqual([
      "operator_request",
      "message_policy_decided",
      "operator_refused",
    ]);
    // wait and cancel on the lead are meaningful and stay.
    expect(await team.wait([lead], { timeoutMs: 3000 })).toMatchObject({
      status: "waited",
      timedOut: false,
    });
    expect(await team.cancel(lead)).toMatchObject({
      status: "cancel_requested",
    });
    await assertTeamReplays(await logOf(store), team.ref.id);
  });

  test("askStatus of an ask the team log never sent is not_found", async () => {
    const { team } = await ran(answers([]));
    const id = AskId.parse("0192b000-0000-7000-8000-0000000000ff:nope");
    expect(await team.askStatus(id)).toEqual({
      status: "not_found",
      askId: id,
    });
  });
});

describe("a handle from openTeam", () => {
  test("drives the team as the run's handle does", async () => {
    const { store, team, lead } = await ran(
      answers([() => say("Drafted.")]),
      undefined,
      "opener",
    );
    const bob = { issuer: "api", tenant: "local", subject: "bob" };
    const opened = unwrap(await openTeam(store, team.ref, { principal: bob }));
    await opened.start("writer", "Draft.");
    const writer = refIn(team, "writer-1");
    expect(await opened.wait([writer])).toMatchObject({
      finished: [{ member: writer, status: "completed", output: "Drafted." }],
    });
    // Held until here: openTeam binds a lead this process defines.
    expect(lead.name).toBe("opener");
    await assertTeamReplays(await logOf(store), team.ref.id);
  });
});

describe("team.wait", () => {
  test("waits for a member to settle, with its result", async () => {
    const { store, team } = await ran(answers([() => say("Drafted.")]));
    await team.start("writer", "Draft.");
    const writer = refIn(team, "writer-1");
    expect(await team.wait([writer])).toEqual({
      status: "waited",
      finished: [{ member: writer, status: "completed", output: "Drafted." }],
      parked: [],
      pending: [],
      timedOut: false,
    });
    await assertTeamReplays(await logOf(store), team.ref.id);
  });

  test("no members, or a mode below 1, is invalid_request and records nothing", async () => {
    const { store, team } = await ran(answers([() => say("Drafted.")]));
    await team.start("writer", "Draft.");
    const writer = refIn(team, "writer-1");
    const before = (await teamLog(store, team)).length;
    const refused = { status: "refused", code: "invalid_request" } as const;
    expect(await team.wait([])).toEqual(refused);
    expect(await team.wait([writer], { mode: 0 })).toEqual(refused);
    expect(await teamLog(store, team)).toHaveLength(before);
  });

  test("mode any returns once one member settles; a mode above the count records nothing", async () => {
    const { store, team } = await ran(
      answers([() => say("Drafted.")]),
      answers([() => say("Read.")]),
    );
    await team.start("writer", "Draft.");
    await team.start("reader", "Read.");
    const both = [refIn(team, "writer-1"), refIn(team, "reader-1")];
    const before = (await teamLog(store, team)).length;
    expect(await team.wait(both, { mode: 3 })).toEqual({
      status: "refused",
      code: "invalid_request",
    });
    expect(await teamLog(store, team)).toHaveLength(before);
    const got = await team.wait(both, { mode: "any" });
    expect(got.status === "waited" && got.finished.length).toBeGreaterThan(0);
    await assertTeamReplays(await logOf(store), team.ref.id);
  });
});

// spec/api.json declares mode and both timeouts PosInt. A public value that is not one is a
// refusal before the writer, never a throw inside the append (AGENTS.md, "Trust boundaries").
describe("a public PosInt option", () => {
  const REFUSED = { status: "refused", code: "invalid_request" } as const;
  const NOT_POS_INT = [
    0,
    1.5,
    -1,
    Number.NaN,
    Number.POSITIVE_INFINITY,
    Number.NEGATIVE_INFINITY,
  ];

  // Two members, so 1.5 sits inside [1, count] and the old range check let it reach the writer.
  test("team.wait refuses every mode that is not a positive integer, and records nothing", async () => {
    const { store, team } = await ran(
      answers([() => say("Drafted.")]),
      answers([() => say("Read.")]),
    );
    await team.start("writer", "Draft.");
    await team.start("reader", "Read.");
    const both = [refIn(team, "writer-1"), refIn(team, "reader-1")];
    const before = (await teamLog(store, team)).length;
    for (const mode of NOT_POS_INT)
      expect(await team.wait(both, { mode })).toEqual(REFUSED);
    expect(await teamLog(store, team)).toHaveLength(before);
  });

  test("team.wait refuses every timeoutMs that is not a positive integer, zero included", async () => {
    const { store, team } = await ran(answers([() => say("Drafted.")]));
    await team.start("writer", "Draft.");
    const writer = refIn(team, "writer-1");
    const before = (await teamLog(store, team)).length;
    for (const timeoutMs of NOT_POS_INT)
      expect(await team.wait([writer], { timeoutMs })).toEqual(REFUSED);
    expect(await teamLog(store, team)).toHaveLength(before);
  });

  test("team.ask refuses every timeoutMs that is not a positive integer, zero included", async () => {
    const { store, team } = await ran(answers([() => say("Drafted.")]));
    await team.start("writer", "Draft.");
    const writer = refIn(team, "writer-1");
    const before = (await teamLog(store, team)).length;
    for (const timeoutMs of NOT_POS_INT)
      expect(await team.ask(writer, "Which topic?", { timeoutMs })).toEqual(
        REFUSED,
      );
    expect(await teamLog(store, team)).toHaveLength(before);
  });
});

describe("team.cancel", () => {
  test("a ref carrying another tenant names no member of this team", async () => {
    const { team } = await ran(answers([() => say("Drafted.")]));
    await team.start("writer", "Draft.");
    const foreign = { ...refIn(team, "writer-1"), tenant: "globex" };
    expect(await team.cancel(foreign)).toEqual({
      status: "refused",
      code: "unknown_member",
    });
  });

  test("a starting member's cancel is durable at once; it ends cancelled without running", async () => {
    let requests = 0;
    const writer = answers([
      () => {
        requests += 1;
        return say("Drafted.");
      },
    ]);
    const { store, team } = await ran(writer);
    await team.start("writer", "Draft.");
    const ref = refIn(team, "writer-1");
    expect(await team.cancel(ref)).toEqual({
      status: "cancel_requested",
      member: ref,
    });
    expect(await team.wait([ref])).toMatchObject({
      finished: [{ member: ref, status: "cancelled" }],
    });
    expect(requests).toBe(0);
    expect(await team.cancel(ref)).toEqual({
      status: "refused",
      code: "member_ended",
    });
    await assertTeamReplays(await logOf(store), team.ref.id);
  });

  test("the operator's cancel of a member the team never had is unknown_member, not forbidden", async () => {
    const { store, team } = await ran(answers([]));
    const got = await team.cancel(refIn(team, "nobody-1"));
    expect(got).toEqual({ status: "refused", code: "unknown_member" });
    expect(types(await teamLog(store, team)).slice(-3)).toEqual([
      "operator_request",
      "message_policy_decided",
      "operator_refused",
    ]);
    await assertTeamReplays(await logOf(store), team.ref.id);
  });
});
