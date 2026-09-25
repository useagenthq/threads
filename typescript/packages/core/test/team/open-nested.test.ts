import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, openTeam, scriptedModel, sqlite } from "../../src";
import { TeamId } from "../../src/log";
import { reading } from "../../src/store/driver";
import { memberRows } from "../../src/team/rows";
import { unwrap } from "../store/helpers";
import { assertTeamReplays } from "./kit";
import { logOf, say, start } from "./run-kit";

// openTeam on a nested lead's team: the nested lead is rebound as its thread was pinned, as a
// member with the defer_tools it inherited from the outer lead.

const OPERATOR = { issuer: "api", tenant: "local", subject: "operator" };
const Teams = z.array(
  z.object({ team_id: TeamId, lead_thread_id: z.string() }),
);

describe("openTeam on a nested team", () => {
  test("rebinds a nested lead that inherited defer_tools, and starts its member", async () => {
    const store = sqlite(":memory:");
    const scanner = agent({
      name: "scanner",
      model: scriptedModel({ responses: [] }),
    });
    // Sets no context: it inherits the outer lead's defer_tools.
    const researcher = agent({
      name: "researcher",
      model: scriptedModel({ responses: [say("Ready.")] }),
      team: [scanner],
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          start("c1", "researcher", "Get ready."),
          say("Started."),
          say("Done."),
        ],
      }),
      context: { defer_tools: "always" },
      team: [researcher],
    });
    const r = await lead.run("Go.", { store });
    const log = await logOf(store);
    const nested = (
      await reading(log.driver, (tx) => memberRows(tx, r.team.ref.id))
    ).find((m) => m.name === "researcher-1");
    const inner = Teams.parse(
      await reading(log.driver, (tx) =>
        tx.all("SELECT team_id, lead_thread_id FROM teams", []),
      ),
    ).find((t) => t.lead_thread_id === nested?.thread_id);
    if (inner === undefined) throw new Error("the nested lead opened its team");
    const ref = { tenant: r.team.ref.tenant, id: inner.team_id };
    const opened = unwrap(await openTeam(store, ref, { principal: OPERATOR }));
    expect((await opened.start("scanner", "Scan.")).status).toBe("started");
    await assertTeamReplays(log, inner.team_id);
    await assertTeamReplays(log, r.team.ref.id);
  });
});
