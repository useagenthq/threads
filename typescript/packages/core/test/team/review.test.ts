import { describe, expect, test } from "bun:test";
import { z } from "zod";
import {
  agent,
  type Model,
  openThread,
  type Principal,
  scriptedModel,
  sqlite,
  type TeamAgent,
  tool,
} from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { TeamWorker } from "../../src/agent/team/worker";
import { BranchId, type TeamId } from "../../src/log";
import { markTestKit } from "../../src/model/guard";
import { memberRows } from "../../src/team/rows";
import { unwrap } from "../store/helpers";
import { assertTeamReplays, query, reading } from "./kit";

type Store = ReturnType<typeof sqlite>;

import {
  call,
  events,
  logOf,
  memberEvents,
  receipts,
  say,
  start,
} from "./run-kit";

// Regressions from the lane 21D review (plans/reviews/claude-lane21d-review.md): a member doing
// real I/O (H1), a member's subagent under the run budget (H2), each member turn under its own
// principal (H3), a parked member whose definition changed (H4), and a closed team (M1).

/** A scripted model whose every answer waits on a real timer, as a network model does. */
function slow(responses: readonly unknown[], ms = 20): Model {
  const base = scriptedModel({ responses: [...responses] });
  const made: Model = {
    ...base,
    send: async function* (...args: Parameters<Model["send"]>) {
      await new Promise((resolve) => setTimeout(resolve, ms));
      yield* base.send(...args);
    },
  };
  markTestKit(made);
  return made;
}

/** A scripted model whose `nth` request (from 1) waits for `open`. */
function waitsOn(
  nth: number,
  responses: readonly unknown[],
  open: Promise<void>,
): Model {
  const base = scriptedModel({ responses: [...responses] });
  let n = 0;
  const made: Model = {
    ...base,
    send: async function* (...args: Parameters<Model["send"]>) {
      n += 1;
      if (n === nth) await open;
      yield* base.send(...args);
    },
  };
  markTestKit(made);
  return made;
}

/** A lead whose researcher parks on an approval to send an email; `instructions` is its deploy. */
function mailer(instructions: string, lead: readonly unknown[]): TeamAgent {
  return agent({
    name: "lead",
    model: scriptedModel({ responses: [...lead] }),
    team: [
      agent({
        name: "researcher",
        instructions,
        model: scriptedModel({
          responses: [
            call("m1", "send_email", { to: "bob" }),
            say("Mailed bob."),
          ],
        }),
        tools: [
          tool({
            name: "send_email",
            description: "Send an email.",
            input: z.object({ to: z.string() }),
            runs: "host",
            execute: async () => "sent",
          }),
        ],
      }),
    ],
  });
}

async function endedWith(store: Store, team: TeamId): Promise<unknown> {
  const member = await memberEvents(store, team, "researcher-1");
  const ended = member.find((e) => e.type === "member_ended");
  return ended?.type === "member_ended" ? ended.data.result : undefined;
}

describe("lane 21D review regressions", () => {
  test("H1: a lead waits for a member whose model answers after real I/O", async () => {
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          start("c1", "researcher", "Go."),
          say("Started."),
          say("Reported."),
        ],
      }),
      team: [agent({ name: "researcher", model: slow([say("Done.")]) })],
    });
    const r = await lead.run("Work.", { store: sqlite(":memory:") });
    expect(r.status === "completed" && r.output).toBe("Reported.");
  }, 5_000);

  test("H2: a member's subagent is charged to the run budget: the sixth of five requests is refused", async () => {
    const store = sqlite(":memory:");
    const helper = agent({
      name: "helper",
      model: scriptedModel({ responses: [say("Helped.")] }),
    });
    const researcher = agent({
      name: "researcher",
      model: scriptedModel({
        responses: [
          call("s1", "spawn_agent", { agent: "helper", prompt: "Help." }),
          say("Done."),
        ],
      }),
      subagents: [helper],
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          start("c1", "researcher", "Go."),
          say("Started."),
          say("Reported."),
        ],
      }),
      team: [researcher],
    });
    const r = await lead.run("Work.", {
      store,
      budget: { max_model_requests: 5 },
    });
    expect(r.status).toBe("budget_exhausted");
    const log = await logOf(store);
    const input = (await events(store, r.thread)).find(
      (e) => e.type === "user_input",
    );
    const charged = z
      .array(z.object({ n: z.int() }))
      .parse(
        await query(
          log.driver,
          "SELECT COUNT(*) AS n FROM budget_ledger WHERE budget_id = ? AND limit_name = 'max_model_requests'",
          [`run:${r.thread.id}:${input?.event_id ?? ""}`],
        ),
      )[0]?.n;
    expect(charged).toBe(5);
    await assertTeamReplays(log, r.team.ref.id);
  });

  test("H3: a member's turn opened by Bob's message acts as Bob, not as Alice who started it", async () => {
    const store = sqlite(":memory:");
    const alice: Principal = {
      issuer: "api",
      tenant: "local",
      subject: "alice",
    };
    const bob: Principal = { issuer: "api", tenant: "local", subject: "bob" };
    const seen: string[] = [];
    const answered = Promise.withResolvers<void>();
    const researcher = agent({
      name: "researcher",
      model: scriptedModel({
        responses: [
          call("w1", "whoami", {}),
          say("Task done."),
          call("w2", "whoami", {}),
          say("Message done."),
        ],
      }),
      tools: [
        tool({
          name: "whoami",
          description: "Who is asking.",
          input: z.object({}),
          runs: "host",
          effect: "read_only",
          execute: async (_input, ctx) => {
            seen.push(ctx.principal.subject);
            if (seen.length === 2) answered.resolve();
            return "ok";
          },
        }),
      ],
    });
    const lead = agent({
      name: "lead",
      // Its last answer waits until the researcher has run Bob's turn.
      model: waitsOn(
        5,
        [
          start("c1", "researcher", "Go."),
          say("Started."),
          say("Reported."),
          call("c2", "send", { to: "researcher-1", text: "One more." }),
          say("Sent."),
          say("Noted."),
        ],
        answered.promise,
      ),
      team: [researcher],
    });
    const first = await lead.run("Work.", { store, principal: alice });
    await lead.run("And more.", {
      store,
      thread: first.thread,
      principal: bob,
    });
    expect(seen).toEqual(["alice", "bob"]);
    await assertTeamReplays(await logOf(store), first.team.ref.id);
  });

  test("H4: a parked member whose definition changed ends failed pin_mismatch, as a value", async () => {
    const store = sqlite(":memory:");
    const rejections: unknown[] = [];
    const onRejection = (e: unknown): void => {
      rejections.push(e);
    };
    process.on("unhandledRejection", onRejection);
    const r = await mailer("v1", [
      start("c1", "researcher", "Mail bob."),
      say("Started."),
    ]).run("Go.", { store });
    expect(r.status).toBe("parked");
    // A deploy changed the researcher's instructions while it waited.
    const again = await mailer("v2", [
      say("The researcher could not go on."),
      say("Next."),
    ]).run("Again.", { store, thread: r.thread });
    expect(again).toMatchObject({ status: "completed", output: "Next." });
    await new Promise((resolve) => setTimeout(resolve, 10));
    process.off("unhandledRejection", onRejection);
    expect(rejections).toEqual([]);
    expect(await endedWith(store, r.team.ref.id)).toMatchObject({
      status: "failed",
      error: { code: "pin_mismatch", message: "rebind failed: pin_mismatch" },
    });
    expect(
      receipts(await events(store, r.thread), "member_ended"),
    ).toHaveLength(1);
    await assertTeamReplays(await logOf(store), r.team.ref.id);
  });

  test("N5: a member whose definition changed with an effect in doubt parks on it, and ends once it is settled", async () => {
    const store = sqlite(":memory:");
    const operator = { issuer: "api", tenant: "local", subject: "operator" };
    const r = await mailer("v1", [
      start("c1", "researcher", "Mail bob."),
      say("Started."),
    ]).run("Go.", { store });
    const log = await logOf(store);
    const row = (
      await reading(log.driver, (tx) => memberRows(tx, r.team.ref.id))
    ).find((m) => m.name === "researcher-1");
    if (row?.branch_id == null) throw new Error("the member has a branch");
    const member = unwrap(await openThread(store, row.thread_id));
    const [pending] = unwrap(await member.pendingApprovals());
    if (pending === undefined) throw new Error("the member waits on approval");
    unwrap(await member.approve(pending.challenge_id, operator));
    // A process crashed right after the email's effect_begin was durable.
    const crashed = unwrap(
      await log.acquire(BranchId.parse(row.branch_id), "crashed"),
    );
    unwrap(
      await crashed.append([
        {
          type: "effect_begin",
          type_version: 1,
          critical: true,
          actor: { kind: "host" },
          data: { call_id: "m1", attempt: 1 },
        },
      ]),
    );
    await crashed.release();
    // A deploy changed the researcher while the email may have been sent.
    const again = await mailer("v2", [say("Unused.")]).run("Again.", {
      store,
      thread: r.thread,
    });
    expect(again.status).toBe("parked");
    const after = await memberEvents(store, r.team.ref.id, "researcher-1");
    expect(after.map((e) => e.type).slice(-2)).toEqual([
      "effect_unknown",
      "parked",
    ]);
    expect(after.some((e) => e.type === "member_ended")).toBe(false);
    expect(after.some((e) => e.type === "tool_result")).toBe(false);
    // A human settles it: the next run ends the member from the record, never re-sending.
    unwrap(
      await member.resolveParked(
        `${row.branch_id}:m1`,
        "assume_done",
        operator,
      ),
    );
    const third = await mailer("v2", [say("Noted."), say("Done.")]).run(
      "Once more.",
      { store, thread: r.thread },
    );
    expect(third.status).toBe("completed");
    const settled = await memberEvents(store, r.team.ref.id, "researcher-1");
    expect(settled.find((e) => e.type === "tool_result")?.data).toMatchObject({
      call_id: "m1",
      origin: "executed",
    });
    expect(await endedWith(store, r.team.ref.id)).toMatchObject({
      status: "failed",
      error: { code: "pin_mismatch" },
    });
    await assertTeamReplays(await logOf(store), r.team.ref.id);
  });

  test("M1: a lead whose run fails returns without waiting on its members' turns", async () => {
    const store = sqlite(":memory:");
    const release = Promise.withResolvers<void>();
    const lead = agent({
      name: "lead",
      // One answer only: the lead's next request fails, which closes the team.
      model: scriptedModel({ responses: [start("c1", "researcher", "Go.")] }),
      team: [
        agent({
          name: "researcher",
          model: waitsOn(1, [say("Late.")], release.promise),
        }),
      ],
    });
    const run = async (): Promise<string> =>
      (await lead.run("Work.", { store })).status;
    const done = await Promise.race([
      run(),
      new Promise<string>((resolve) =>
        setTimeout(() => resolve("waited"), 2_000),
      ),
    ]);
    release.resolve();
    expect(done).toBe("failed");
  }, 5_000);

  test("M2: a member whose agent this process lacks ends failed pin_unavailable", async () => {
    const store = sqlite(":memory:");
    const r = await mailer("v1", [
      start("c1", "researcher", "Mail bob."),
      say("Started."),
    ]).run("Go.", { store });
    expect(r.status).toBe("parked");
    const opened = await openStore(store);
    const worker = new TeamWorker({
      store,
      log: opened.log,
      artifacts: opened.artifacts,
      team: r.team.ref.id,
      agents: new Map(),
    });
    await worker.start();
    await worker.stop();
    expect(await endedWith(store, r.team.ref.id)).toMatchObject({
      status: "failed",
      error: { code: "pin_unavailable" },
    });
    await assertTeamReplays(opened.log, r.team.ref.id);
  });
});
