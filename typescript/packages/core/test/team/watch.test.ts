import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, type Model, scriptedModel, sqlite } from "../../src";
import { storeOf } from "../../src/agent/sqlite";
import type { KnownEvent } from "../../src/log";
import { markTestKit } from "../../src/model/guard";
import { LogStore, memoryArtifacts } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { TEAM_CONSTANTS } from "../../src/team/constants";
import { unwrap } from "../store/helpers";
import { assertTeamReplays } from "./kit";
import {
  call,
  events,
  logOf,
  memberEvents,
  receipts,
  say,
  start,
  types,
} from "./run-kit";

// wait and monitor in a running team, and the deadlines of asks and waits (spec/schema/README.md,
// "Teams", Waits and monitors; design §4.12 and §4.13). A wait parks its caller until every
// listed member settles, or until its deadline, when it returns what settled so far.

const Preview = z.object({ status: z.string() }).loose();

function resultOf(log: readonly KnownEvent[], callId: string): unknown {
  const found = log.findLast(
    (e) => e.type === "tool_result" && e.data.call_id === callId,
  );
  return found?.type === "tool_result"
    ? Preview.parse(JSON.parse(found.data.preview))
    : undefined;
}

/**
 * A store on a clock the test moves (deadlines are read from the store's clock). Time passing
 * also renews every live lease, as each holder's renewal timer would have.
 */
function clocked() {
  const clock = { now: Date.now() };
  const artifacts = memoryArtifacts();
  const db = openBunSqlite(":memory:");
  const log = unwrap(LogStore.open(db, () => clock.now, artifacts));
  const elapse = (ms: number): void => {
    db.run(
      "UPDATE leases SET expires_at = expires_at + ? WHERE expires_at > ?",
      [ms, clock.now],
    );
    clock.now += ms;
  };
  return { store: storeOf({ log, artifacts }), elapse };
}

/** A scripted model whose first request waits for `open`. */
function held(open: Promise<void>, responses: readonly unknown[]): Model {
  const base = scriptedModel({ responses: [...responses] });
  let first = true;
  const made: Model = {
    ...base,
    send: async function* (...args: Parameters<Model["send"]>) {
      if (first) {
        first = false;
        await open;
      }
      yield* base.send(...args);
    },
  };
  markTestKit(made);
  return made;
}

/** Resolves once `check` holds, polling. */
async function until(check: () => Promise<boolean>): Promise<void> {
  for (let i = 0; i < 500; i += 1) {
    if (await check()) return;
    await new Promise((r) => setTimeout(r, 10));
  }
  throw new Error("timed out waiting");
}

describe("wait", () => {
  test("the lead waits for two members: both results, in the wait's member order", async () => {
    const store = sqlite(":memory:");
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          start("c1", "researcher", "Sales."),
          start("c2", "researcher", "Batteries."),
          call("c3", "wait", { members: ["researcher-1", "researcher-2"] }),
          say("Both reported."),
          ...Array.from({ length: 3 }, () => say("Final.")),
        ],
      }),
      team: [
        agent({
          name: "researcher",
          model: scriptedModel({
            responses: [say("Sales rose."), say("Prices fell.")],
          }),
        }),
      ],
    });
    const r = await lead.run("Research.", { store });
    expect(r.status === "completed" && r.output).toBe("Final.");
    const log = await events(store, r.thread);
    const waited = z
      .object({
        finished: z.array(z.object({ member: z.object({ name: z.string() }) })),
      })
      .parse(resultOf(log, "c3"));
    expect(waited.finished.map((f) => f.member.name)).toEqual([
      "researcher-1",
      "researcher-2",
    ]);
    expect(resultOf(log, "c3")).toMatchObject({
      status: "waited",
      timed_out: false,
    });
    assertTeamReplays(await logOf(store), r.team.ref.id);
  });

  test("at the deadline a wait returns what settled so far, timed out; the member reports later", async () => {
    const { store, elapse } = clocked();
    const release = Promise.withResolvers<void>();
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          start("c1", "researcher", "Go."),
          call("c2", "wait", { members: ["researcher-1"] }),
          say("It is still working."),
          say("Final."),
        ],
      }),
      team: [
        agent({
          name: "researcher",
          model: held(release.promise, [say("Done.")]),
        }),
      ],
    });
    const run = lead.run("Research.", { store });
    const parkedOnWait = async (): Promise<boolean> => {
      const log = await logOf(store);
      return (
        log.driver.all("SELECT 1 FROM monitors WHERE kind = 'settle'", [])
          .length > 0
      );
    };
    await until(parkedOnWait);
    elapse(TEAM_CONSTANTS.askWaitDefaultMs);
    const log = await logOf(store);
    await until(async () => (await parkedOnWait()) === false);
    release.resolve();
    const r = await run;
    expect(r.status === "completed" && r.output).toBe("Final.");
    const lead0 = await events(store, r.thread);
    expect(resultOf(lead0, "c2")).toMatchObject({
      status: "waited",
      finished: [],
      pending: [{ name: "researcher-1" }],
      timed_out: true,
    });
    // The settlement after the deadline sent no settle notice for the finished wait.
    const member = await memberEvents(store, r.team.ref.id, "researcher-1");
    const sent = member.filter(
      (e) =>
        e.type === "message_sent" && e.data.envelope.kind === "member_settled",
    );
    expect(sent).toHaveLength(1);
    assertTeamReplays(log, r.team.ref.id);
  });
});

describe("ask deadline", () => {
  test("an ask no one answers closes timed out at its deadline, and the turn goes on", async () => {
    const { store, elapse } = clocked();
    const release = Promise.withResolvers<void>();
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          start("c1", "researcher", "Go."),
          call("c2", "ask", { to: "researcher-1", question: "Topic?" }),
          say("No answer."),
          say("Final."),
          say("Final."),
        ],
      }),
      team: [
        agent({
          name: "researcher",
          model: held(release.promise, [
            say("Done."),
            say("Too late to answer."),
          ]),
        }),
      ],
    });
    const run = lead.run("Ask.", { store });
    const open = async (): Promise<boolean> => {
      const log = await logOf(store);
      return (
        log.driver.all("SELECT 1 FROM asks WHERE state = 'open'", []).length > 0
      );
    };
    await until(open);
    elapse(TEAM_CONSTANTS.askWaitDefaultMs);
    await until(async () => (await open()) === false);
    release.resolve();
    const r = await run;
    expect(r.status === "completed" && r.output).toBe("Final.");
    const log = await events(store, r.thread);
    expect(resultOf(log, "c2")).toMatchObject({ status: "timed_out" });
    assertTeamReplays(await logOf(store), r.team.ref.id);
  });
});

describe("monitor", () => {
  test("a monitored member that ends wakes the idle lead with its result", async () => {
    const store = sqlite(":memory:");
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          start("c1", "researcher", "Go."),
          call("c2", "monitor", { member: "researcher-1" }),
          say("Watching."),
          say("It failed."),
          say("It failed."),
        ],
      }),
      // No answer scripted: its request fails, and the member ends failed.
      team: [
        agent({ name: "researcher", model: scriptedModel({ responses: [] }) }),
      ],
    });
    const r = await lead.run("Watch.", { store });
    expect(r.status === "completed" && r.output).toBe("It failed.");
    const log = await events(store, r.thread);
    expect(resultOf(log, "c2")).toMatchObject({ status: "monitoring" });
    const ended = receipts(log, "member_ended");
    // One notice for the end monitor, one for the start's task monitor.
    expect(
      ended
        .map((e) =>
          e.type === "message_received"
            ? e.data.envelope.monitor_id?.split(":").at(-1)
            : "",
        )
        .toSorted(),
    ).toEqual(["researcher-1", "task"]);
    expect(types(log)).toContain("monitor_set");
    assertTeamReplays(await logOf(store), r.team.ref.id);
  });

  test("monitoring a member that already ended returns its result at once", async () => {
    const store = sqlite(":memory:");
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          start("c1", "researcher", "Go."),
          say("Started."),
          call("c2", "monitor", { member: "researcher-1" }),
          say("It had failed."),
        ],
      }),
      team: [
        agent({ name: "researcher", model: scriptedModel({ responses: [] }) }),
      ],
    });
    const r = await lead.run("Watch.", { store });
    expect(r.status === "completed").toBe(true);
    const log = await events(store, r.thread);
    expect(resultOf(log, "c2")).toMatchObject({
      status: "ended",
      result: { status: "failed" },
    });
    expect(types(log)).toContain("member_observed");
    assertTeamReplays(await logOf(store), r.team.ref.id);
  });
});
