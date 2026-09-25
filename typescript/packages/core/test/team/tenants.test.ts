import { describe, expect, test } from "bun:test";
import { agent, scriptedModel, sqlite } from "../../src";
import { tenantStore } from "../../src/agent/sqlite";
import { assertTeamReplays } from "./kit";
import { call, events, logOf, say, start } from "./run-kit";

// Tenants (design §2.6, "Tenant"): a team lives in its lead's tenant, frozen into every ref, and
// nothing crosses tenants: a lead of another tenant, on the same database, can't address its
// members; each team's rows and replay stay its own.

describe("teams and tenants", () => {
  test("a member of another tenant's team is not addressable", async () => {
    const root = sqlite(":memory:");
    const acme = tenantStore(root, "acme");
    const globex = tenantStore(root, "globex");
    const researcher = agent({
      name: "researcher",
      model: scriptedModel({ responses: [say("Done.")] }),
    });
    const first = await agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          start("c1", "researcher", "Go."),
          say("Started."),
          say("Final."),
        ],
      }),
      team: [researcher],
    }).run("Work.", { store: acme });
    expect(first.status === "completed" && first.team.ref.tenant).toBe("acme");

    const other = await agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          call("c1", "send", { to: "researcher-1", text: "Hello?" }),
          say("No one there."),
        ],
      }),
      team: [],
    }).run("Try.", { store: globex });
    expect(other.team.ref.tenant).toBe("globex");
    const result = (await events(globex, other.thread)).find(
      (e) => e.type === "tool_result",
    );
    expect(result?.type === "tool_result" && result.data.preview).toBe(
      '{"code":"unknown_member","status":"refused"}',
    );
    await assertTeamReplays(await logOf(acme), first.team.ref.id);
    await assertTeamReplays(await logOf(globex), other.team.ref.id);
  });
});
