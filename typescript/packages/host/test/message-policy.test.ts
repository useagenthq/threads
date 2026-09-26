import { afterEach, describe, expect, test } from "bun:test";
import { agent, ConfigError, scriptedModel, sqlite } from "@threads/core";
import {
  openStore,
  pinnedLine0,
  sha256Hex,
  storeConnection,
  tenantStore,
} from "@threads/core/host";
import { z } from "zod";
import { assertTeamReplays } from "../../core/test/team/kit";
import { host } from "../src/host";
import {
  alice,
  type Harness,
  harness,
  knownEventsOf,
  say,
  sseMessages,
  use,
} from "./kit";
import { sqlAll } from "./sql";

// The host's messagePolicy (spec/api.json host.message_policy; lane 29 section C): its setup
// refusals, the team tools a rule gives an agent in line 0, a rule that makes its from a lead, and
// the member budget a start rule caps. Every runtime test ends with the team's replay check.

let h: Harness | undefined;
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
});

const quiet = (...responses: readonly unknown[]) =>
  scriptedModel({ responses: [...responses] });

const billing = agent({ name: "billing", model: quiet(say("Paid.")) });

const Accepted = z.object({ thread_id: z.string(), run_id: z.string() });
const Result = z.object({
  result: z.object({ status: z.string(), output: z.string().optional() }),
});

/** Starts a run of `agent` over the API and returns its thread and its last SSE result. */
async function ran(
  live: Harness,
  name: string,
  input: string,
): Promise<{ readonly thread: string; readonly output: string }> {
  const accepted = Accepted.parse(
    await (
      await live.call("POST", "/v1/runs", {
        as: alice,
        body: { agent: name, input },
        headers: { "idempotency-key": `k-${name}` },
      })
    ).json(),
  );
  const last = (
    await sseMessages(
      await live.call(
        "GET",
        `/v1/threads/${accepted.thread_id}/runs/${accepted.run_id}/events`,
        { as: alice },
      ),
    )
  ).at(-1);
  const done = Result.parse(last).result;
  expect(done.status).toBe("completed");
  return { thread: accepted.thread_id, output: done.output ?? "" };
}

const Branches = z.array(z.object({ branch_id: z.string() }));

async function mainBranch(live: Harness, thread: string): Promise<string> {
  const branches = Branches.parse(
    await (
      await live.call("GET", `/v1/threads/${thread}/branches`, { as: alice })
    ).json(),
  );
  return branches[0]?.branch_id ?? "";
}

const Ids = z.array(z.object({ budget_id: z.string() }));

const TEAM_TOOLS = [
  "ask",
  "cancel",
  "monitor",
  "reply",
  "send",
  "start",
  "wait",
];

function thrownBy(make: () => unknown): ConfigError {
  try {
    make();
  } catch (error) {
    if (error instanceof ConfigError) return error;
    throw error;
  }
  throw new Error("host() did not refuse");
}

describe("host({messagePolicy}) at setup", () => {
  const support = agent({ name: "support", model: quiet() });

  test("a from or to that names no host agent is refused", () => {
    const bad = thrownBy(() =>
      host({
        store: sqlite(":memory:"),
        agents: { support, billing },
        messagePolicy: [{ from: "support", to: "payroll", allow: ["ask"] }],
      }),
    );
    expect(bad.code).toBe("invalid_config");
    expect(bad.message).toContain("payroll is not a host agent");
  });

  test("an empty allow is refused, naming the rule", () => {
    const bad = thrownBy(() =>
      host({
        store: sqlite(":memory:"),
        agents: { support, billing },
        messagePolicy: [{ from: "support", to: "billing", allow: [] }],
      }),
    );
    expect(bad.code).toBe("invalid_config");
    expect(bad.message).toContain(
      "messagePolicy rule support -> billing: allow is empty",
    );
  });

  test("a repeated (from, to) pair is refused duplicate_name", () => {
    const bad = thrownBy(() =>
      host({
        store: sqlite(":memory:"),
        agents: { support, billing },
        messagePolicy: [
          { from: "support", to: "billing", allow: ["ask"] },
          { from: "support", to: "billing", allow: ["send"] },
        ],
      }),
    );
    expect(bad.code).toBe("duplicate_name");
    expect(bad.message).toContain("is listed twice");
  });
});

describe("the team tools a rule gives an agent", () => {
  test("a rule from with only ask sees only ask in line 0", async () => {
    const support = agent({
      name: "support",
      model: quiet(say("Asking billing.")),
    });
    h = harness({
      agents: { support, billing },
      messagePolicy: [{ from: "support", to: "billing", allow: ["ask"] }],
    });
    const run = await ran(h, "support", "Is INV-1001 paid?");
    const events = await knownEventsOf(
      h.store,
      alice.tenant,
      await mainBranch(h, run.thread),
    );
    const started = events.find((e) => e.type === "thread_started");
    if (started?.type !== "thread_started")
      throw new Error("a thread opens with its pin");
    const pinned = started.data.tools.map((t) => t.name);
    expect(pinned.filter((n) => TEAM_TOOLS.includes(n))).toEqual(["ask"]);
    // Invariant 5: every request of the epoch declares the same line-0 bytes, and those bytes
    // are the pin's, which shows ask and no other team tool.
    const declared = events.flatMap((e) =>
      e.type === "model_request" ? [e.data.declared_prefix.sha256] : [],
    );
    expect(declared.length).toBeGreaterThan(0);
    const line0 = pinnedLine0(started.data);
    const digest = sha256Hex(new TextEncoder().encode(`${line0}\n`));
    expect(new Set(declared)).toEqual(new Set([digest]));
    expect(line0).toContain('"name":"ask"');
    expect(line0).not.toContain('"name":"send"');
  });

  test("an agent no rule names gets no team tool", async () => {
    const support = agent({
      name: "support",
      model: quiet(say("No team here.")),
    });
    h = harness({
      agents: { support, billing },
      messagePolicy: [{ from: "billing", to: "support", allow: ["send"] }],
    });
    const run = await ran(h, "support", "Hello.");
    const events = await knownEventsOf(
      h.store,
      alice.tenant,
      await mainBranch(h, run.thread),
    );
    const started = events.find((e) => e.type === "thread_started");
    if (started?.type !== "thread_started")
      throw new Error("a thread opens with its pin");
    expect(
      started.data.tools
        .map((t) => t.name)
        .filter((n) => TEAM_TOOLS.includes(n)),
    ).toEqual([]);
  });
});

describe("a rule that allows start makes its from a lead", () => {
  test("its thread opens a team, lists the agent and starts it", async () => {
    const support = agent({
      name: "support",
      model: quiet(
        use("start", { agent: "billing", task: "Is INV-1001 paid?" }, "c1"),
        say("Asked billing."),
        say("Billing says paid."),
      ),
    });
    h = harness({
      agents: { support, billing },
      messagePolicy: [
        { from: "support", to: "billing", allow: ["start", "ask"] },
      ],
    });
    const run = await ran(h, "support", "Is INV-1001 paid?");
    expect(run.output).toBe("Billing says paid.");
    const branch = await mainBranch(h, run.thread);
    const events = await knownEventsOf(h.store, alice.tenant, branch);
    const started = events.find((e) => e.type === "thread_started");
    if (started?.type !== "thread_started")
      throw new Error("a thread opens with its pin");
    // A rule-only lead has no team of its own, so the rule alone opens one and fills the listing.
    const team = started.data.team;
    if (team === undefined)
      throw new Error("a lead's thread_started names its team");
    expect(started.data.instructions).toContain(
      "Agents you can start as team members with start: billing.",
    );
    expect(
      started.data.tools
        .map((t) => t.name)
        .filter((n) => TEAM_TOOLS.includes(n))
        .toSorted(),
    ).toEqual(["ask", "start"]);
    expect(events.map((e) => e.type)).toContain("member_started");
    const { log } = await openStore(tenantStore(h.store, alice.tenant));
    await assertTeamReplays(log, team.id);
  });

  test("the rule's budget is the started member's own", async () => {
    const support = agent({
      name: "support",
      model: quiet(
        use("start", { agent: "billing", task: "Is INV-1001 paid?" }, "c1"),
        say("Asked billing."),
        say("Billing says paid."),
      ),
    });
    h = harness({
      agents: { support, billing },
      messagePolicy: [
        {
          from: "support",
          to: "billing",
          allow: ["start"],
          // A scripted model has no price, so a cost limit can bound no attempt; requests can.
          budget: { max_model_requests: 3 },
        },
      ],
    });
    const run = await ran(h, "support", "Is INV-1001 paid?");
    const events = await knownEventsOf(
      h.store,
      alice.tenant,
      await mainBranch(h, run.thread),
    );
    const started = events.find((e) => e.type === "member_started");
    if (started?.type !== "member_started")
      throw new Error("the start records the member");
    expect(started.data.budget).toEqual({ max_model_requests: 3 });
    // And it covers the member: every request of its turns reserves against the cap.
    const { db } = await storeConnection(tenantStore(h.store, alice.tenant));
    const reserved = Ids.parse(
      await sqlAll(db, "SELECT DISTINCT budget_id FROM budget_ledger", []),
    );
    expect(reserved.map((r) => r.budget_id)).toContain(
      `start:${started.event_id}`,
    );
  });
});
