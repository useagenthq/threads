import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { parseLogLine } from "../../src/log";
import { checkNotYetPhase2 } from "../../src/validate/phase2";

// The supervision forms of Teams Phase 2, each one line of the shared team wire vector, are
// refused by this reader as unsupported_critical_event until the supervision build (lane 29E).
// Every other Phase 2 form is live (lane 29D) and passes this gate.

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

function event(name: string): Parameters<typeof checkNotYetPhase2>[0] {
  const line = lines.get(name);
  if (line === undefined) throw new Error(`no vector line ${name}`);
  const parsed = parseLogLine(line);
  if (!parsed.ok || parsed.value.kind !== "event")
    throw new Error(`${name} is not an event line`);
  return parsed.value.event;
}

const REFUSED: ReadonlyArray<readonly [string, string]> = [
  ["supervisor_decided", "supervisor_decided"],
  [
    "member_started of a host member restarting generation 1",
    "member_started{restart_of}",
  ],
];

/** Live in lane 29D: the reader reduces these and semantic rules 50 and 52-55 decide them. */
const LIVE: readonly string[] = [
  "team_opened of a host team",
  "member_started of a host member",
  "thread_started of a host member",
  "member_idle{turn_failed}",
  "ask_closed failed",
  "budget_exceeded of a hop cap",
  "a caller's ask",
  "a host member's reply to a caller",
  "a receipt to a caller",
  "a turn_failed bounce to a member",
  "team_opened",
];

describe("supervision forms before lane 29E", () => {
  for (const [name, form] of REFUSED)
    test(`${name} is refused as ${form}`, () => {
      const refused = checkNotYetPhase2(event(name));
      expect(refused?.code).toBe("unsupported_critical_event");
      expect(refused?.message.startsWith(`${form} is not reduced`)).toBe(true);
    });

  for (const name of LIVE)
    test(`${name} passes the gate`, () => {
      expect(checkNotYetPhase2(event(name))).toBeUndefined();
    });
});
