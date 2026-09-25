import { describe, expect, test } from "bun:test";
import { agent, type Model, scriptedModel, sqlite } from "../../src";
import { markTestKit } from "../../src/model/guard";
import { assertTeamReplays, query } from "./kit";
import {
  call,
  events,
  logOf,
  memberEvents,
  receipts,
  resultOf,
  say,
  start,
  types,
} from "./run-kit";

// cancel in a running team (spec/schema/README.md, "Teams"; design §4.14): the starter's request
// is durable intent; the member's writer applies it as control mail (its receipt and
// cancel_requested{scope: tree}), releasing its parks, and the member ends cancelled. A model
// call in flight is aborted at once (N1). Every test ends with the team replaying from its logs.

/** A scripted model whose `n`th request (from 1) first waits for `gates[n]`. */
function gated(
  responses: readonly unknown[],
  gates: ReadonlyMap<number, Promise<void>>,
): Model {
  const base = scriptedModel({ responses: [...responses] });
  let n = 0;
  const made: Model = {
    ...base,
    send: async function* (...args: Parameters<Model["send"]>) {
      n += 1;
      await gates.get(n);
      yield* base.send(...args);
    },
  };
  markTestKit(made);
  return made;
}

/** A model whose request never answers: it ends only when its run's signal aborts it. */
function hanging(began: () => void): Model {
  const base = scriptedModel({ responses: [] });
  const made: Model = {
    ...base,
    send: async function* (_request, _context, options) {
      began();
      const aborted = Promise.withResolvers<never>();
      options?.signal?.addEventListener("abort", () =>
        aborted.reject(new Error("aborted")),
      );
      await aborted.promise;
    },
  };
  markTestKit(made);
  return made;
}

const finals = Array.from({ length: 6 }, () => say("Final."));

describe("cancel", () => {
  test("a member whose model call is in flight is stopped at once and ends cancelled", async () => {
    const store = sqlite(":memory:");
    const began = Promise.withResolvers<void>();
    const lead = agent({
      name: "lead",
      model: gated(
        [
          start("c1", "researcher", "Read."),
          call("c2", "cancel", { member: "researcher-1" }),
          ...finals,
        ],
        new Map([[2, began.promise]]),
      ),
      team: [agent({ name: "researcher", model: hanging(began.resolve) })],
    });
    const r = await lead.run("Go.", { store });
    expect(r.status).toBe("completed");
    expect(resultOf(await events(store, r.thread), "c2")).toMatchObject({
      status: "cancel_requested",
      member: { name: "researcher-1", generation: 1 },
    });
    const member = await memberEvents(store, r.team.ref.id, "researcher-1");
    expect(receipts(member, "cancel")).toHaveLength(1);
    expect(types(member)).toContain("cancel_requested");
    const ended = member.find((e) => e.type === "member_ended");
    expect(ended?.type === "member_ended" && ended.data.result.status).toBe(
      "cancelled",
    );
    await assertTeamReplays(await logOf(store), r.team.ref.id);
  }, 15_000);

  test("a parked asker's cancel closes its ask cancelled, and it ends", async () => {
    const store = sqlite(":memory:");
    const asked = Promise.withResolvers<void>();
    const release = Promise.withResolvers<void>();
    const lead = agent({
      name: "lead",
      model: gated(
        [
          start("c1", "researcher", "Read."),
          start("c2", "writer", "Ask researcher-1."),
          call("c3", "cancel", { member: "writer-1" }),
          ...finals,
        ],
        new Map([[3, asked.promise]]),
      ),
      team: [
        agent({
          name: "researcher",
          model: gated(
            [say("Read."), say("Late.")],
            new Map([[1, release.promise]]),
          ),
        }),
        agent({
          name: "writer",
          model: gated(
            [call("w1", "ask", { to: "researcher-1", question: "?" })],
            new Map(),
          ),
        }),
      ],
    });
    const run = lead.run("Go.", { store });
    // The lead's cancel waits until the writer's ask is open and parked.
    for (let i = 0; i < 500; i += 1) {
      const log = await logOf(store);
      const open = await query(
        log.driver,
        "SELECT 1 FROM team_members WHERE name = 'writer-1' AND state = 'parked'",
        [],
      );
      if (open.length > 0) break;
      await new Promise((r) => setTimeout(r, 10));
    }
    asked.resolve();
    release.resolve();
    const r = await run;
    expect(r.status).toBe("completed");
    const writer = await memberEvents(store, r.team.ref.id, "writer-1");
    const closed = writer.find((e) => e.type === "ask_closed");
    expect(closed?.type === "ask_closed" && closed.data.outcome).toEqual({
      status: "cancelled",
    });
    expect(resultOf(writer, "w1")).toMatchObject({ status: "cancelled" });
    const ended = writer.find((e) => e.type === "member_ended");
    expect(ended?.type === "member_ended" && ended.data.result.status).toBe(
      "cancelled",
    );
    await assertTeamReplays(await logOf(store), r.team.ref.id);
  }, 15_000);

  test("a member that didn't start the target is forbidden, and nothing is sent", async () => {
    const store = sqlite(":memory:");
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          start("c1", "researcher", "Read."),
          start("c2", "writer", "Cancel researcher-1."),
          ...finals,
        ],
      }),
      team: [
        agent({
          name: "researcher",
          model: scriptedModel({ responses: [say("Read.")] }),
        }),
        agent({
          name: "writer",
          model: scriptedModel({
            responses: [
              call("w1", "cancel", { member: "researcher-1" }),
              say("I may not."),
            ],
          }),
        }),
      ],
    });
    const r = await lead.run("Go.", { store });
    expect(r.status).toBe("completed");
    const writer = await memberEvents(store, r.team.ref.id, "writer-1");
    expect(resultOf(writer, "w1")).toEqual({
      code: "forbidden",
      status: "refused",
    });
    const researcher = await memberEvents(store, r.team.ref.id, "researcher-1");
    expect(receipts(researcher, "cancel")).toHaveLength(0);
    await assertTeamReplays(await logOf(store), r.team.ref.id);
  });
});
