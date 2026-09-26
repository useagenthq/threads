import { afterEach, describe, expect, test } from "bun:test";
import {
  agent,
  type MemberRef,
  openTeam,
  type Store,
  scriptedModel,
  sqlite,
} from "@threads/core";
import {
  type KnownEvent,
  type Principal,
  storeConnection,
} from "@threads/core/host";
import { z } from "zod";
import { MemberName } from "../../core/src/log";
import { type Host, host } from "../src";
import { HostContext } from "../src/context";
import { Teams } from "../src/team-worker";
import { Watch } from "../src/watch";
import { authenticate, knownEventsOf, say, until, use } from "./kit";
import { sqlAll, sqlRun } from "./sql";
import { assertReplays, teamOf } from "./team-kit";

// The host's team tick, driven one pass at a time (design §7 Phase 2 A, and the 29A crash
// drills): a claim another host took leaves the mail alone until it expires, the consume that
// wakes an idle lead happens exactly once, a turn it left open is resumed by recovery with no
// second turn, and the operator's cancel reaches a member through the host's worker alone.

const LOCAL: Principal = {
  issuer: "local",
  tenant: "local",
  subject: "local",
};

const hosts: Host[] = [];
const driving: Teams[] = [];
afterEach(async () => {
  for (const t of driving.splice(0)) await t.stop();
  for (const h of hosts.splice(0)) await h.stop();
});

/** A lead that ran once, with a helper the operator then started, and no host anywhere. */
async function seeded(name: string) {
  const helper = agent({
    name: `${name}_helper`,
    model: scriptedModel({
      responses: [
        use("send", { to: `${name}_lead`, text: "The scan is clean." }, "s1"),
        say("Reported."),
      ],
    }),
  });
  const lead = agent({
    name: `${name}_lead`,
    model: scriptedModel({
      responses: [say("Standing by."), say("The scan is clean.")],
    }),
    team: [helper],
  });
  const store = sqlite(":memory:");
  const ran = await lead.run("Stand by.", { store });
  const started = await ran.team.start(`${name}_helper`, "Scan the deps.");
  if (started.status !== "started")
    throw new Error(`the start was ${started.status}`);
  return {
    store,
    lead,
    team: await teamOf(store, LOCAL.tenant, ran.thread.branch),
    branch: ran.thread.branch,
  };
}

type Seeded = Awaited<ReturnType<typeof seeded>>;

/** A team tick of its own, as the host's runs it, but one pass at a time. */
function driver(store: Store, lead: Seeded["lead"]): Teams {
  const teams = new Teams(new HostContext(store, { lead }, {}));
  driving.push(teams);
  return teams;
}

const MailRow = z.object({
  mail_id: z.string(),
  state: z.string(),
  to_name: z.string().nullable(),
});

async function mail(store: Store): Promise<readonly z.infer<typeof MailRow>[]> {
  const { db } = await storeConnection(store);
  return z
    .array(MailRow)
    .parse(
      await sqlAll(
        db,
        "SELECT mail_id, state, to_name FROM mail ORDER BY created_at",
      ),
    );
}

/** The mail waiting for the lead, once its helper has sent it. */
async function toLead(store: Store, name: string): Promise<string> {
  await until(async () =>
    (await mail(store)).some(
      (m) => m.to_name === `${name}_lead` && m.state === "pending",
    ),
  );
  const row = (await mail(store)).find((m) => m.to_name === `${name}_lead`);
  if (row === undefined) throw new Error("no mail to the lead");
  return row.mail_id;
}

const received = (events: readonly KnownEvent[]): number =>
  events.filter((e) => e.type === "message_received").length;

const turns = (events: readonly KnownEvent[]): number =>
  events.filter((e) => e.type === "turn_completed").length;

describe("a claim taken before the lease", () => {
  test("the mail waits for the claim to expire, and there is one consume", async () => {
    const { store, lead, team, branch } = await seeded("claim");
    const teams = driver(store, lead);
    await teams.pass(new Watch());
    const mailId = await toLead(store, "claim");
    const { db } = await storeConnection(store);

    // A host claimed the row and died before taking the lease: nothing else consumes it.
    await sqlRun(
      db,
      "UPDATE mail SET claim_token = 'dead-host', claim_expires_at = ? WHERE mail_id = ?",
      [Date.now() + 60_000, mailId],
    );
    const watch = new Watch();
    await teams.pass(watch);
    const events = () => knownEventsOf(store, LOCAL.tenant, branch);
    expect(received(await events())).toBe(0);
    expect(watch.entries()).toHaveLength(0);

    // The claim runs out (its TTL is 30 s): the next pass consumes it, once.
    await sqlRun(db, "UPDATE mail SET claim_expires_at = 0 WHERE mail_id = ?", [
      mailId,
    ]);
    await teams.pass(watch);
    expect(received(await events())).toBe(1);
    expect(watch.entries()).toHaveLength(1);
    await teams.pass(watch);
    expect(received(await events())).toBe(1);
    expect((await mail(store)).find((m) => m.mail_id === mailId)?.state).toBe(
      "consumed",
    );
    await teams.stop();
    await assertReplays(store, LOCAL.tenant, team);
  }, 20_000);
});

describe("a crash after the consume, before the woken turn's first request", () => {
  test("recovery resumes the turn, with no second turn", async () => {
    const { store, lead, team, branch } = await seeded("resume");
    const teams = driver(store, lead);
    await teams.pass(new Watch());
    await toLead(store, "resume");
    // The consume opens the lead's turn; the host that made it never ran it.
    await teams.pass(new Watch());
    const events = () => knownEventsOf(store, LOCAL.tenant, branch);
    expect(received(await events())).toBe(1);
    expect(turns(await events())).toBe(1);
    await teams.stop();

    const served = host({ store, authenticate, agents: { lead } });
    hosts.push(served);
    await served.ready();
    await until(async () => turns(await events()) === 2, 10_000);
    // The turn is run once: a later tick finds nothing open.
    await until(async () =>
      (await mail(store)).every((m) => m.state !== "pending"),
    );
    const all = await events();
    expect(turns(all)).toBe(2);
    expect(received(all)).toBe(1);
    expect(all.filter((e) => e.type === "user_input")).toHaveLength(1);
    await assertReplays(store, LOCAL.tenant, team);
  }, 20_000);
});

describe("the operator's cancel of a member", () => {
  test("is applied by the host's worker, with no lead run anywhere", async () => {
    const { store, lead, team } = await seeded("cancel");
    const opened = await openTeam(
      store,
      { tenant: LOCAL.tenant, id: team },
      { principal: LOCAL },
    );
    if (!opened.ok) throw new Error(opened.error.message);
    const member: MemberRef = {
      tenant: LOCAL.tenant,
      team,
      name: MemberName.parse("cancel_helper-1"),
      generation: 1,
    };
    const asked = await opened.value.cancel(member);
    expect(asked.status).toBe("cancel_requested");

    const teams = driver(store, lead);
    await teams.pass(new Watch());
    await until(async () => {
      const members = await opened.value.members();
      return members.some(
        (m) => m.name === "cancel_helper-1" && m.state === "ended",
      );
    }, 10_000);
    await teams.stop();
    expect(
      (await opened.value.members()).find((m) => m.name === "cancel_helper-1")
        ?.result?.status,
    ).toBe("cancelled");
    await assertReplays(store, LOCAL.tenant, team);
  }, 20_000);
});

describe("stop()", () => {
  test("leaves every row durable, and the next host finishes the work", async () => {
    const { store, lead, team, branch } = await seeded("stopped");
    const first = driver(store, lead);
    await first.pass(new Watch());
    await toLead(store, "stopped");
    await first.stop();
    const events = () => knownEventsOf(store, LOCAL.tenant, branch);
    expect(received(await events())).toBe(0);
    // Nothing is claimed for good: the row is still pending, with its envelope intact.
    expect((await mail(store)).some((m) => m.state === "pending")).toBe(true);

    const served = host({ store, authenticate, agents: { lead } });
    hosts.push(served);
    await served.ready();
    await until(async () => turns(await events()) === 2, 10_000);
    expect(received(await events())).toBe(1);
    await assertReplays(store, LOCAL.tenant, team);
  }, 20_000);
});
