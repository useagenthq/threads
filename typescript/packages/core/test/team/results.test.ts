import { expect, test } from "bun:test";
import type {
  AskOutcome,
  MonitorResult,
  Waited,
  Wire,
} from "../../src/team/results";

// The team tools' results as the model sees them (spec/schema/README.md, "Model tools"): the
// public types' Wire form, snake_case all the way down. The tool code builds its values as these
// types, so a field renamed on one side fails to compile on the other.

test("a result's wire form is snake_case, and ids are plain strings", () => {
  const timedOut: Wire<AskOutcome> = { status: "timed_out", ask_id: "b:c2" };
  const waited: Wire<Waited> = {
    status: "waited",
    finished: [],
    parked: [],
    pending: [],
    timed_out: true,
  };
  const ended: Wire<MonitorResult> = {
    status: "ended",
    result: {
      member: { tenant: "t", team: "team", name: "a-1", generation: 1 },
      status: "handed_off",
      to_thread: "thread",
    },
  };
  // @ts-expect-error the model never sees the camelCase field
  const camel: Wire<AskOutcome> = { status: "timed_out", askId: "b:c2" };
  expect([timedOut, waited, ended, camel]).toHaveLength(4);
});
