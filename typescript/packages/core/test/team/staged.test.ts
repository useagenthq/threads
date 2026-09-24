import { describe, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { loadCase } from "../conformance/cases";
import { runTeam } from "../conformance/team";
import { liftTeamRefusal, STAGED } from "./kit";

// The `team` runner over the staged team cases. They move into the corpus once lane 21A's rules
// accept their logs; until then the pre-build refusal is lifted here, so the runner's own steps
// (rule 43, the index fold, the tree walk) are proven now.

liftTeamRefusal();

const TEAM_CASES = readdirSync(STAGED)
  .filter(
    (name) =>
      JSON.parse(readFileSync(join(STAGED, name, "case.json"), "utf8")).kind ===
      "team",
  )
  .toSorted();

/**
 * The lead's row there ends `running` at a turn a received notification opens, which only 21A's
 * fold knows. test.failing turns red the day 21A lands, so this list is emptied then.
 */
const NEEDS_21A: ReadonlySet<string> = new Set(["team-failed-rebind-bounces"]);

describe("staged team cases", () => {
  for (const name of TEAM_CASES) {
    // States wait for 21A too: it renders received mail and opens turns on it.
    const run = () => runTeam(loadCase(name, STAGED), { states: false });
    if (NEEDS_21A.has(name)) test.failing(name, run);
    else test(name, run);
  }
});
