import { describe, expect, test } from "bun:test";
import { z } from "zod";
import {
  type Agent,
  agent,
  assertNever,
  ConfigError,
  type StartResult,
  scriptedModel,
  sqlite,
  type TeamAgent,
  tool,
} from "../../src";
import type { Team } from "../../src/agent/team/types";
import { assertTeamReplays } from "./kit";
import { logOf, say, start } from "./run-kit";

// agent({team}) (spec/api.json agent.team): a TeamAgent, whose run() and stream() results carry
// the team; setup refuses a member that hands off and a team tool's name.

const model = () => scriptedModel({ responses: [say("Hi.")] });

describe("agent({team})", () => {
  test("the overloads: no team is an Agent, team [] or a list is a TeamAgent", async () => {
    const plain: Agent<undefined, string> = agent({ model: model() });
    const lead: TeamAgent<undefined, string> = agent({
      model: model(),
      team: [],
    });
    const r = await plain.run("Hi.", { store: sqlite(":memory:") });
    // @ts-expect-error a plain Agent's run result carries no team
    expect(r.team).toBeUndefined();
    const led = await lead.run("Hi.", { store: sqlite(":memory:") });
    const team: Team = led.team;
    expect(team.ref.id).toMatch(/^[0-9a-f-]{36}$/);
    const typed = agent({
      model: model(),
      output: z.object({ n: z.number() }),
      team: [],
    });
    const check: TeamAgent<undefined, { n: number }> = typed;
    expect(check.name).toBe("agent");
  });

  test("stream(): the committed events, then a result with the team", async () => {
    const store = sqlite(":memory:");
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          start("c1", "researcher", "Go."),
          say("Started."),
          say("Done."),
        ],
      }),
      team: [agent({ name: "researcher", model: model() })],
    });
    const run = lead.stream("Work.", { store });
    const seen: string[] = [];
    for await (const item of run)
      if (item.kind === "event") seen.push(item.event.type);
    const result = await run.result;
    expect(result.status === "completed" && result.output).toBe("Done.");
    expect(seen).toContain("member_started");
    expect(result.team.ref.tenant).toBe((await logOf(store)).tenant);
    assertTeamReplays(await logOf(store), result.team.ref.id);
  });

  test("check(): a member that hands off is refused handoff_in_team", async () => {
    const target = agent({ name: "billing", model: model() });
    const lead = agent({
      name: "lead",
      model: model(),
      team: [agent({ name: "desk", model: model(), handoffs: [target] })],
    });
    expect(await lead.check()).toMatchObject({
      ok: false,
      error: { code: "handoff_in_team" },
    });
    await expect(
      lead.run("Hi.", { store: sqlite(":memory:") }),
    ).rejects.toThrow(ConfigError);
  });

  test("check(): an own tool named like a team tool is refused duplicate_name, naming it", async () => {
    const ask = tool({
      name: "ask",
      description: "Ask someone.",
      input: z.object({ q: z.string() }),
      runs: "host",
      effect: "read_only",
      execute: async () => "ok",
    });
    const lead = agent({ model: model(), tools: [ask], team: [] });
    const checked = await lead.check();
    expect(checked).toMatchObject({
      ok: false,
      error: { code: "duplicate_name" },
    });
    expect(checked.ok ? "" : checked.error.message).toContain("tool ask");
    // Outside a team the name is free.
    expect(await agent({ model: model(), tools: [ask] }).check()).toEqual({
      ok: true,
      value: undefined,
    });
    // A listed member's own tool is refused at the lead's setup too.
    const withMember = agent({
      model: model(),
      team: [agent({ name: "desk", model: model(), tools: [ask] })],
    });
    const member = await withMember.check();
    expect(member).toMatchObject({
      ok: false,
      error: { code: "duplicate_name" },
    });
    expect(member.ok ? "" : member.error.message).toContain("agent desk");
  });

  test("check(): two agents of one name in a team tree are refused duplicate_name", async () => {
    const lead = agent({
      model: model(),
      team: [
        agent({ name: "writer", model: model() }),
        agent({ name: "writer", model: model() }),
      ],
    });
    expect(await lead.check()).toMatchObject({
      ok: false,
      error: { code: "duplicate_name" },
    });
  });

  test("assertNever ends a switch over a tool result's statuses", () => {
    const describe = (r: StartResult): string => {
      switch (r.status) {
        case "started":
          return r.member.name;
        case "refused":
          return r.code;
        default:
          return assertNever(r);
      }
    };
    expect(describe({ status: "refused", code: "team_closed" })).toBe(
      "team_closed",
    );
    // @ts-expect-error a value outside the union reaches assertNever only at run time
    expect(() => assertNever("surprise")).toThrow();
  });
});
