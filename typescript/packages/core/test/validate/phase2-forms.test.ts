import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { parseLogLine } from "../../src/log";
import { checkNotYetPhase2 } from "../../src/validate/phase2";

// Each Teams Phase 2 form, one line of the shared team wire vector, is refused by this reader as
// unsupported_critical_event until the Phase 2 build (spec/schema/README.md, "Teams Phase 2").

const VECTOR = join(
  import.meta.dir,
  "../../../../../spec/conformance/vectors/team-wire.json",
);
const Doc = z.object({
  cases: z.array(z.object({ name: z.string(), line: z.string() })),
});
const lines = new Map(
  Doc.parse(JSON.parse(readFileSync(VECTOR, "utf8"))).cases.map((c) => [
    c.name,
    c.line,
  ]),
);

const FORMS: ReadonlyArray<readonly [string, string]> = [
  ["team_opened of a host team", "team_opened{kind: host}"],
  ["member_started of a host member", "member_started{host_member}"],
  ["thread_started of a host member", "thread_started{host_member}"],
  ["supervisor_decided", "supervisor_decided"],
  ["member_idle{turn_failed}", "member_idle{turn_failed}"],
  ["ask_closed failed", "ask_closed{failed}"],
  ["budget_exceeded of a hop cap", "budget_exceeded{scope: hop}"],
  ["a caller's ask", "a caller's mail"],
  ["a host member's reply to a caller", "mail to a caller"],
  ["a receipt to a caller", "mail to a caller"],
  ["a turn_failed bounce to a member", "a turn_failed bounce"],
];

describe("Phase 2 forms before the build", () => {
  for (const [name, form] of FORMS)
    test(`${name} is refused as ${form}`, () => {
      const line = lines.get(name);
      if (line === undefined) throw new Error(`no vector line ${name}`);
      const parsed = parseLogLine(line);
      if (!parsed.ok || parsed.value.kind !== "event")
        throw new Error(`${name} is not an event line`);
      const refused = checkNotYetPhase2(parsed.value.event);
      expect(refused?.code).toBe("unsupported_critical_event");
      expect(refused?.message.startsWith(`${form} is not reduced`)).toBe(true);
    });

  test("a Phase 1 team event passes", () => {
    const line = lines.get("team_opened");
    if (line === undefined) throw new Error("no team_opened line");
    const parsed = parseLogLine(line);
    if (!parsed.ok || parsed.value.kind !== "event")
      throw new Error("team_opened is not an event line");
    expect(checkNotYetPhase2(parsed.value.event)).toBeUndefined();
  });
});
