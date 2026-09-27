import { afterEach, describe, expect, test } from "bun:test";
import { agent, ConfigError, scriptedModel, sqlite, tool } from "@threads/core";
import { hostTeamIds, storeConnection } from "@threads/core/host";
import { z } from "zod";
import type { HostOptions } from "../src/host";
import { host } from "../src/host";
import { alice, type Harness, harness, say } from "./kit";
import { sqlAll } from "./sql";

// host({members}) and Host.team (spec/api.json host.members, Host.team; lane 29D): every setup
// refusal, the lazy open of a tenant's host team at its derived ids, and the handle on it.

let h: Harness | undefined;
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
});

const quiet = (...responses: readonly unknown[]) =>
  scriptedModel({ responses: [...responses] });

/** A host member with nothing that could stop for a human: no approvers needed. */
const billing = agent({ name: "billing", model: quiet(say("Paid.")) });
const support = agent({ name: "support", model: quiet(say("Asked.")) });

function thrownBy(make: () => unknown): ConfigError {
  try {
    make();
  } catch (error) {
    if (error instanceof ConfigError) return error;
    throw error;
  }
  throw new Error("host() did not refuse");
}

type Agents = HostOptions["agents"];
type Members = NonNullable<HostOptions["members"]>;
type Rules = NonNullable<HostOptions["messagePolicy"]>;

const withMembers = (
  agents: Agents,
  members: Members,
  messagePolicy: Rules = [],
): ConfigError =>
  thrownBy(() =>
    host({ store: sqlite(":memory:"), agents, members, messagePolicy }),
  );

describe("host({members}) at setup", () => {
  test("a member naming no host agent is refused", () => {
    const bad = withMembers({ billing }, { payroll: {} });
    expect(bad.code).toBe("invalid_config");
    expect(bad.message).toContain("members.payroll names no host agent");
  });

  test("a host member with a team of its own is refused", () => {
    const lead = agent({ name: "lead", model: quiet(), team: [billing] });
    const bad = withMembers({ lead, billing }, { lead: {} });
    expect(bad.code).toBe("invalid_config");
    expect(bad.message).toContain("members.lead has a team of its own");
  });

  test("a host member with handoffs is refused handoff_in_team", () => {
    const handing = agent({
      name: "handing",
      model: quiet(),
      handoffs: [billing],
    });
    const bad = withMembers({ handing, billing }, { handing: {} });
    expect(bad.code).toBe("handoff_in_team");
    expect(bad.message).toContain("members.handing lists handoffs");
  });

  test("a host member that turns compaction off is refused", () => {
    const never = agent({
      name: "never",
      model: quiet(),
      context: {
        compact: {
          trigger: { permille: 1000 },
          keep_tail: { tokens: 20_000 },
          max_failures: 3,
        },
      },
    });
    const bad = withMembers({ never }, { never: {} });
    expect(bad.code).toBe("invalid_config");
    expect(bad.message).toContain("members.never turns compaction off");
  });

  // The other spelling of the same thing. Only the permille form was covered, in either language,
  // which is how TypeScript came to accept an agent Python refused.
  test("a host member whose token trigger is past its window is refused", () => {
    const never = agent({
      name: "never",
      model: quiet(),
      context: {
        compact: {
          trigger: { tokens: 10_000_000 },
          keep_tail: { tokens: 20_000 },
          max_failures: 3,
        },
      },
    });
    const bad = withMembers({ never }, { never: {} });
    expect(bad.code).toBe("invalid_config");
    expect(bad.message).toContain("members.never turns compaction off");
  });

  test("a host member with a writing tool and no approvers is refused", () => {
    const writes = agent({
      name: "writes",
      model: quiet(),
      tools: [
        tool({
          name: "touch",
          description: "Writes.",
          input: z.object({}),
          effect: "idempotent",
          dedupWindowMs: 60_000,
          runs: "host",
          execute: async () => "ok",
        }),
      ],
    });
    const bad = withMembers({ writes }, { writes: {} });
    expect(bad.code).toBe("invalid_config");
    expect(bad.message).toBe(
      "a host member is shared by every conversation in the tenant, so its approvals need approvers; add approvers to agent 'writes'",
    );
  });

  test("the same member with approvers is accepted", () => {
    const writes = agent({
      name: "writes",
      model: quiet(),
      approvers: [alice],
      tools: [
        tool({
          name: "touch",
          description: "Writes.",
          input: z.object({}),
          effect: "idempotent",
          dedupWindowMs: 60_000,
          runs: "host",
          execute: async () => "ok",
        }),
      ],
    });
    expect(() =>
      host({
        store: sqlite(":memory:"),
        agents: { writes },
        members: { writes: {} },
      }),
    ).not.toThrow();
  });

  test("withinMs below a second is refused", () => {
    const bad = withMembers({ billing }, { billing: { withinMs: 999 } });
    expect(bad.code).toBe("invalid_config");
    expect(bad.message).toContain("a restart window is at least 1000 ms");
  });

  // Both options are the shared PosInt check, so the table is the one Python's
  // test_host_member_config.py runs: past MAX_SAFE_INTEGER the wire can't hold the value, and a
  // string or a boolean is what an untyped caller really passes.
  const NOT_POS_INT = [0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, true, "3"];

  for (const field of ["maxRestarts", "withinMs"] as const)
    test(`${field} takes the positive-integer check`, () => {
      for (const value of NOT_POS_INT) {
        // The computed key is what lets a boolean and a string through the checker, which is what
        // an untyped caller really passes.
        const bad = withMembers({ billing }, { billing: { [field]: value } });
        expect(bad.code).toBe("invalid_config");
        expect(bad.message).toContain(
          `${field} is ${value}; give a positive integer`,
        );
      }
    });

  test("a rule to a host member allowing monitor is refused", () => {
    const bad = withMembers({ support, billing }, { billing: {} }, [
      { from: "support", to: "billing", allow: ["ask", "monitor"] },
    ]);
    expect(bad.code).toBe("invalid_config");
    expect(bad.message).toContain("it allows only send and ask, not monitor");
  });

  test("a rule from one host member to another allowing cancel is refused", () => {
    const hr = agent({ name: "hr", model: quiet() });
    const bad = withMembers({ hr, billing }, { billing: {}, hr: {} }, [
      { from: "billing", to: "hr", allow: ["cancel"] },
    ]);
    expect(bad.code).toBe("invalid_config");
    expect(bad.message).toContain(
      "one host member may only send to or ask another",
    );
  });

  test("a rule FROM a host member allowing start is refused: it would make it a lead too", () => {
    const bad = withMembers({ support, billing }, { billing: {} }, [
      { from: "billing", to: "support", allow: ["start"] },
    ]);
    expect(bad.code).toBe("invalid_config");
    expect(bad.message).toContain(
      "a host member owns no members to start, monitor or cancel, and a start rule would make it a lead as well",
    );
    expect(bad.message).toContain("not start");
  });

  test("a rule FROM a host member to a plain agent may still send and ask", () => {
    expect(() =>
      host({
        store: sqlite(":memory:"),
        agents: { support, billing },
        members: { billing: {} },
        messagePolicy: [
          { from: "billing", to: "support", allow: ["send", "ask"] },
        ],
      }),
    ).not.toThrow();
  });

  test("a rule between two plain agents is untouched by the host-member check", () => {
    expect(() =>
      host({
        store: sqlite(":memory:"),
        agents: { support, billing },
        members: {},
        messagePolicy: [
          { from: "support", to: "billing", allow: ["start", "ask"] },
        ],
      }),
    ).not.toThrow();
  });

  test("a rule to a host member allowing send and ask is accepted", () => {
    expect(() =>
      host({
        store: sqlite(":memory:"),
        agents: { support, billing },
        members: { billing: {} },
        messagePolicy: [
          { from: "support", to: "billing", allow: ["send", "ask"] },
        ],
      }),
    ).not.toThrow();
  });
});

const Row = z.object({
  team_id: z.string(),
  tenant_id: z.string(),
  kind: z.string(),
  lead_thread_id: z.string().nullable(),
  team_log_branch_id: z.string(),
});
const MemberRow = z.object({
  name: z.string(),
  role: z.string(),
  generation: z.number(),
  state: z.string(),
  branch_id: z.string().nullable(),
});

async function teams(live: Harness): Promise<readonly z.infer<typeof Row>[]> {
  const { db } = await storeConnection(live.store);
  return z
    .array(Row)
    .parse(
      await sqlAll(
        db,
        "SELECT team_id, tenant_id, kind, lead_thread_id, team_log_branch_id FROM teams",
      ),
    );
}

describe("the host team of a tenant", () => {
  test("ready() opens it at its derived ids, with one member row per configured name", async () => {
    h = harness({
      agents: { support, billing },
      members: { billing: {} },
      messagePolicy: [{ from: "support", to: "billing", allow: ["ask"] }],
    });
    await h.host.ready();
    const ids = hostTeamIds("local");
    const rows = await teams(h);
    expect(rows).toEqual([
      {
        team_id: ids.teamId,
        tenant_id: "local",
        kind: "host",
        lead_thread_id: null,
        team_log_branch_id: ids.branchId,
      },
    ]);
    const { db } = await storeConnection(h.store);
    const members = z
      .array(MemberRow)
      .parse(
        await sqlAll(
          db,
          "SELECT name, role, generation, state, branch_id FROM team_members",
        ),
      );
    expect(members).toEqual([
      {
        name: "billing",
        role: "host_member",
        generation: 1,
        state: "starting",
        branch_id: null,
      },
    ]);
  });

  test("a second ready() is already_open: one team_opened, one member_started", async () => {
    const store = sqlite(":memory:");
    const first = harness({
      store,
      agents: { billing },
      members: { billing: {} },
    });
    await first.host.ready();
    await first.host.stop();
    const second = harness({
      store,
      agents: { billing },
      members: { billing: {} },
    });
    await second.host.ready();
    await second.host.stop();
    const ids = hostTeamIds("local");
    const { db } = await storeConnection(store);
    const types = z
      .array(z.object({ type: z.string() }))
      .parse(
        await sqlAll(
          db,
          "SELECT type FROM events WHERE branch_id = ? ORDER BY seq",
          [ids.branchId],
        ),
      )
      .map((r) => r.type);
    expect(types).toEqual(["team_opened", "member_started"]);
  });

  test("Host.team is not_found without a members option, and a handle with one", async () => {
    const bare = harness({ agents: { billing } });
    await bare.host.ready();
    const none = await bare.host.team({ principal: alice });
    expect(none.ok).toBe(false);
    if (!none.ok) expect(none.error.code).toBe("not_found");
    await bare.host.stop();

    h = harness({ agents: { billing }, members: { billing: {} } });
    const got = await h.host.team({ principal: alice });
    expect(got.ok).toBe(true);
    if (!got.ok) throw new Error(got.error.message);
    expect(got.value.ref).toEqual({
      tenant: alice.tenant,
      id: hostTeamIds(alice.tenant).teamId,
    });
    // A host team is leadless, so nothing starts a member in it.
    expect(await got.value.start("billing", "hi")).toEqual({
      status: "refused",
      code: "forbidden",
    });
    expect((await got.value.members()).map((m) => String(m.name))).toEqual([
      "billing",
    ]);
  });

  test("two tenants get two host teams, and neither names the other's", async () => {
    h = harness({ agents: { billing }, members: { billing: {} } });
    await h.host.ready();
    // Each tenant's host team opens on its first Host.team, at the ids that tenant derives.
    for (const tenant of [alice.tenant, "beta"])
      expect((await h.host.team({ principal: { ...alice, tenant } })).ok).toBe(
        true,
      );
    const rows = await teams(h);
    expect(rows.map((r) => r.tenant_id).toSorted()).toEqual(
      ["beta", "local", alice.tenant].toSorted(),
    );
    expect(new Set(rows.map((r) => r.team_id)).size).toBe(rows.length);
    for (const row of rows)
      expect(row.team_id).toBe(hostTeamIds(row.tenant_id).teamId);
  });
});
