import { describe, expect, test } from "bun:test";
import {
  agent,
  type MemberRef,
  openTeam,
  scriptedModel,
  sqlite,
  type Team,
  type TeamItem,
} from "../../src";
import { memberEntry } from "../../src/agent/registry";
import { openStore } from "../../src/agent/sqlite";
import { teamHandle } from "../../src/agent/team/handle";
import { takeTeamLogMail } from "../../src/agent/team/log-mail";
import { MemberName, TeamId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import type { LogStore } from "../../src/store";
import { rebuildTeamIndex } from "../../src/team/rebuild";
import { teamRow } from "../../src/team/rows";
import { unwrap } from "../store/helpers";
import { assertTeamReplays } from "./kit";
import { logOf, say, types } from "./run-kit";

// The operator's handle (spec/api.json Team; design §4.4): start and send as operator requests
// in the team log, their idempotency keys, the busy bound, and the pure reads members() and
// events(). Every test ends with the team's replay check.

const ALICE = { issuer: "api", tenant: "local", subject: "alice" } as const;
const BOB = { issuer: "api", tenant: "local", subject: "bob" } as const;
/** The run's own principal: the local operator run() records by default. */
const OPERATOR = { issuer: "api", tenant: "local", subject: "operator" };

function lead(writerAnswers: readonly string[] = ["Draft."]) {
  return agent({
    name: "lead",
    model: scriptedModel({
      responses: [say("Ready."), say("Noted."), say("Noted.")],
    }),
    team: [
      agent({
        name: "writer",
        model: scriptedModel({ responses: writerAnswers.map((t) => say(t)) }),
      }),
    ],
  });
}

/** A lead that ran once, and its team's handle as the run's principal. */
async function ran(store = sqlite(":memory:")) {
  const agentLead = lead();
  const r = await agentLead.run("Get ready.", { store });
  return { store, r, team: r.team, lead: agentLead };
}

async function teamLog(store: ReturnType<typeof sqlite>, team: Team) {
  const log = await logOf(store);
  const row = teamRow(log.driver, team.ref.id);
  if (row === undefined) throw new Error("no team");
  return knownEvents(unwrap(log.read(row.team_log_branch_id)));
}

const refIn = (team: Team, name: string): MemberRef => ({
  tenant: team.ref.tenant,
  team: team.ref.id,
  name: MemberName.parse(name),
  generation: 1,
});

async function collect(items: AsyncIterable<TeamItem>): Promise<TeamItem[]> {
  const out: TeamItem[] = [];
  for await (const item of items) out.push(item);
  return out;
}

describe("team.start and team.send", () => {
  test("team.start is one operator request; the member starts with its label", async () => {
    const { store, team } = await ran();
    const started = await team.start("writer", "Draft the summary.", {
      label: "drafter",
    });
    expect(started).toEqual({
      status: "started",
      member: refIn(team, "writer-1"),
    });
    expect(types(await teamLog(store, team))).toEqual([
      "team_opened",
      "operator_request",
      "message_policy_decided",
      "member_started",
      "message_sent",
    ]);
    const members = await team.members();
    expect(members.map((m) => [m.name, m.state, m.label])).toEqual([
      ["lead", "idle", undefined],
      ["writer-1", "starting", "drafter"],
    ]);
    expect(members[0]?.result).toEqual({
      member: refIn(team, "lead"),
      status: "completed",
      output: "Ready.",
    });
    assertTeamReplays(await logOf(store), team.ref.id);
  });

  test("the lead's next run materializes and runs an operator's member", async () => {
    const { store, r, team, lead: agentLead } = await ran();
    await team.start("writer", "Draft the summary.");
    await agentLead.run("Continue.", { store, thread: r.thread });
    const writer = (await team.members()).find((m) => m.name === "writer-1");
    expect(writer?.state).toBe("idle");
    expect(writer?.result).toEqual({
      member: refIn(team, "writer-1"),
      status: "completed",
      output: "Draft.",
    });
    // Its task notification goes to the team log, which takes it as a receipt only.
    const log = await logOf(store);
    takeTeamLogMail(
      log,
      (await openStore(store)).artifacts,
      team.ref.id,
      undefined,
    );
    const received = (await teamLog(store, team)).filter(
      (e) => e.type === "message_received",
    );
    expect(received.map((e) => e.type)).toEqual(["message_received"]);
    assertTeamReplays(log, team.ref.id);
  });

  test("an unknown agent and a member not started yet are refused, and logged", async () => {
    const { store, team } = await ran();
    expect(await team.start("editor", "Go.")).toEqual({
      status: "refused",
      code: "unknown_agent",
    });
    const sent = await team.send(refIn(team, "writer-1"), "Hello.");
    expect(sent).toEqual({ status: "refused", code: "unknown_member" });
    const log = await teamLog(store, team);
    expect(types(log).filter((t) => t === "operator_refused")).toHaveLength(2);
    assertTeamReplays(await logOf(store), team.ref.id);
  });

  test("team.send reaches a started member once; its key replays the outcome", async () => {
    const { store, team } = await ran();
    await team.start("writer", "Draft.");
    const first = await team.send(refIn(team, "writer-1"), "Keep it short.", {
      idempotencyKey: "s1",
    });
    expect(first.status).toBe("sent");
    const before = (await teamLog(store, team)).length;
    // The caller never saw the outcome: the same key, principal and body replay it.
    expect(
      await team.send(refIn(team, "writer-1"), "Keep it short.", {
        idempotencyKey: "s1",
      }),
    ).toEqual(first);
    expect(await teamLog(store, team)).toHaveLength(before);
    // Another body under the key is refused, recorded without the key.
    expect(
      await team.send(refIn(team, "writer-1"), "Longer.", {
        idempotencyKey: "s1",
      }),
    ).toEqual({ status: "refused", code: "idempotency_key_reused" });
    const bob = unwrap(await openTeam(store, team.ref, { principal: BOB }));
    expect(
      await bob.send(refIn(team, "writer-1"), "Keep it short.", {
        idempotencyKey: "s1",
      }),
    ).toEqual({
      status: "refused",
      code: "idempotency_key_principal_mismatch",
    });
    const requests = (await teamLog(store, team)).filter(
      (e) => e.type === "operator_request",
    );
    expect(
      requests.map((e) =>
        e.type === "operator_request" ? e.data.idempotency_key : "",
      ),
    ).toEqual([undefined, "s1", undefined, undefined]);
    assertTeamReplays(await logOf(store), team.ref.id);
  });
});

describe("the busy bound", () => {
  test("a held team-log lease past the bound is busy, and nothing is recorded", async () => {
    const { store, team, lead } = await ran();
    const log = await logOf(store);
    const row = teamRow(log.driver, team.ref.id);
    const entry = memberEntry(lead);
    if (row === undefined || entry === undefined) throw new Error("no team");
    const held = unwrap(log.acquire(row.team_log_branch_id, "someone-else"));
    const { artifacts } = await openStore(store);
    const bounded = teamHandle({
      log,
      artifacts,
      ref: team.ref,
      principal: ALICE,
      lead: entry,
      busyBoundMs: 30,
    });
    const before = (await teamLog(store, team)).length;
    expect(await bounded.start("writer", "Go.")).toEqual({
      status: "refused",
      code: "busy",
    });
    expect(await teamLog(store, team)).toHaveLength(before);
    held.release();
    // Released: the same request goes through.
    expect((await bounded.start("writer", "Go.")).status).toBe("started");
    assertTeamReplays(log, team.ref.id);
  });
});

describe("openTeam", () => {
  test("another principal of the tenant acts through its own handle", async () => {
    const { store, team } = await ran();
    const opened = unwrap(await openTeam(store, team.ref, { principal: BOB }));
    const started = await opened.start("writer", "Draft.");
    expect(started.status).toBe("started");
    const request = (await teamLog(store, team)).find(
      (e) => e.type === "operator_request",
    );
    expect(request?.actor.principal).toEqual(BOB);
    assertTeamReplays(await logOf(store), team.ref.id);
  });

  test("two leads of one name: openTeam binds the one that ran, by its config_hash", async () => {
    const { store, team } = await ran();
    // Defined after the run, with the same name and another team.
    agent({
      name: "lead",
      model: scriptedModel({ responses: [] }),
      team: [
        agent({ name: "hacker", model: scriptedModel({ responses: [] }) }),
      ],
    });
    const opened = unwrap(await openTeam(store, team.ref, { principal: BOB }));
    expect((await opened.start("writer", "Draft.")).status).toBe("started");
    expect(await opened.start("hacker", "Go.")).toEqual({
      status: "refused",
      code: "unknown_agent",
    });
    assertTeamReplays(await logOf(store), team.ref.id);
  });

  test("a process that doesn't define the lead that ran gets unavailable, and nothing is logged", async () => {
    const store = sqlite(":memory:");
    const other = agent({
      name: "lead",
      model: scriptedModel({ responses: [say("Ready.")] }),
      team: [
        agent({ name: "editor", model: scriptedModel({ responses: [] }) }),
      ],
    });
    const r = await other.run("Get ready.", { store });
    const before = (await teamLog(store, r.team)).length;
    // Stand-in for another process: this lead's definition is gone, another of its name is here.
    const log = await logOf(store);
    log.driver.run(
      "UPDATE team_members SET config_hash = ? WHERE role = 'lead'",
      ["0".repeat(64)],
    );
    const opened = await openTeam(store, r.team.ref, { principal: BOB });
    expect(!opened.ok && opened.error.code).toBe("unavailable");
    expect(await teamLog(store, r.team)).toHaveLength(before);
  });

  test("another tenant is forbidden and an unknown team is not_found", async () => {
    const { store, team } = await ran();
    const other = { issuer: "api", tenant: "globex", subject: "m" };
    const forbidden = await openTeam(store, team.ref, { principal: other });
    expect(!forbidden.ok && forbidden.error.code).toBe("forbidden");
    const missing = await openTeam(
      store,
      {
        tenant: "local",
        id: TeamId.parse("0192c000-0000-7000-8000-00000000ffff"),
      },
      { principal: ALICE },
    );
    expect(!missing.ok && missing.error.code).toBe("not_found");
  });
});

describe("team.events", () => {
  test("the feed: team_opened, the lead's events, then the operator's request", async () => {
    const { store, team } = await ran();
    await team.start("writer", "Draft.");
    const items = await collect(team.events());
    const kinds = items.map((i) =>
      i.kind === "event" ? `${i.source.kind}:${i.event.type}` : i.kind,
    );
    // A new team's feed starts with team_opened, then the lead's events (design §5).
    expect(kinds.slice(0, 2)).toEqual([
      "team:team_opened",
      "member:thread_started",
    ]);
    expect(kinds.slice(-4)).toEqual([
      "operator:operator_request",
      "operator:message_policy_decided",
      "operator:member_started",
      "operator:message_sent",
    ]);
    const last = items.at(-1);
    expect(last?.kind === "event" && last.source).toEqual({
      kind: "operator",
      principal: OPERATOR,
      request: expect.any(String),
    });
    // Resuming after a cursor yields exactly the rest, each once.
    const third = items[2];
    if (third === undefined) throw new Error("three items");
    const rest = await collect(team.events({ after: third.cursor }));
    expect(rest).toEqual(items.slice(3));
    assertTeamReplays(await logOf(store), team.ref.id);
  });

  test("a feed read while the team grows past a page yields what was committed at the start", async () => {
    const { store, team } = await ran();
    // 120 refused starts: three feed rows each, more than one page.
    for (let i = 0; i < 120; i += 1) await team.start("editor", "Go.");
    const committed = (await collect(team.events())).length;
    let seen = 0;
    for await (const _item of team.events()) {
      seen += 1;
      if (seen === 1) await team.start("writer", "Draft.");
    }
    expect(seen).toBe(committed);
    expect(await collect(team.events())).toHaveLength(committed + 4);
    assertTeamReplays(await logOf(store), team.ref.id);
  });

  test("a rebuilt feed restarts: epoch_restarted, then the whole new epoch", async () => {
    const { store, team } = await ran();
    const before = await collect(team.events());
    const log: LogStore = await logOf(store);
    unwrap(rebuildTeamIndex(log, team.ref.id));
    const cursor = before.at(-1)?.cursor;
    if (cursor === undefined) throw new Error("a feed");
    const after = await collect(team.events({ after: cursor }));
    expect(after[0]).toEqual({
      kind: "epoch_restarted",
      cursor: { epoch: 2, offset: 0 },
    });
    expect(
      after.slice(1).map((i) => i.kind === "event" && i.event.event_id),
    ).toEqual(
      expect.arrayContaining(
        before.map((i) => i.kind === "event" && i.event.event_id),
      ),
    );
    expect(after).toHaveLength(before.length + 1);
    assertTeamReplays(log, team.ref.id);
  });
});
