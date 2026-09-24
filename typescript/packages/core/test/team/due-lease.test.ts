import { expect, test } from "bun:test";
import { z } from "zod";
import { agent, scriptedModel } from "../../src";
import { BranchId } from "../../src/log";
import { TEAM_CONSTANTS } from "../../src/team/constants";
import { clocked, held, until } from "./clock-kit";
import { assertTeamReplays } from "./kit";
import { call, memberEvents, resultOf, say, start } from "./run-kit";

// A parked member's ask is past its deadline while another process holds the member's lease
// (reviewer probe H2, 21E.1): the holder closes it, so this worker leaves the member alone and
// its event loop keeps turning. Once the lease is free, the worker wakes the member and the ask
// closes timed out.

test("a due ask under another holder's lease: the worker yields, then closes it once free", async () => {
  const { store, log, elapse } = clocked();
  const release = Promise.withResolvers<void>();
  const researcher = agent({
    name: "researcher",
    model: held(release.promise, [say("Done."), say("Late.")]),
  });
  const writer = agent({
    name: "writer",
    model: scriptedModel({
      responses: [
        call("w1", "ask", { to: "researcher-1", question: "Topic?" }),
        say("Report."),
      ],
    }),
  });
  const lead = agent({
    name: "lead",
    model: scriptedModel({
      responses: [
        start("c1", "researcher", "Go."),
        start("c2", "writer", "Ask researcher-1."),
        say("Started."),
        ...Array.from({ length: 8 }, () => say("Final.")),
      ],
    }),
    team: [researcher, writer],
  });
  const run = lead.run("Go.", { store });
  const parked = () =>
    z
      .array(z.object({ branch_id: z.string() }))
      .parse(
        log.driver.all(
          "SELECT branch_id FROM team_members WHERE name = 'writer-1' AND state = 'parked'",
          [],
        ),
      );
  await until(async () => parked().length > 0);
  const branch = BranchId.parse(parked()[0]?.branch_id);
  const other = log.acquire(branch, "another-process");
  if (!other.ok) throw new Error("the writer's lease is free once it parks");
  elapse(TEAM_CONSTANTS.askWaitDefaultMs);

  // Several worker polls pass under the other lease; a worker that relaunched the member on
  // microtasks would starve this timer (and the test would hang).
  const window = 4 * TEAM_CONSTANTS.wakePollInProcessMs;
  const t0 = performance.now();
  await new Promise((r) => setTimeout(r, window));
  expect(performance.now() - t0).toBeLessThan(window + 2_000);

  other.value.release();
  const open = async (): Promise<boolean> =>
    log.driver.all("SELECT 1 FROM asks WHERE state = 'open'", []).length > 0;
  await until(async () => (await open()) === false);
  release.resolve();
  const r = await run;
  expect(r.status).toBe("completed");
  const w = await memberEvents(store, r.team.ref.id, "writer-1");
  expect(resultOf(w, "w1")).toMatchObject({ status: "timed_out" });
  assertTeamReplays(log, r.team.ref.id);
}, 20_000);
