import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { authorize } from "../../src/agent/config";
import { callSpec, toolSpec } from "../../src/loop/turn";
import { knownEvents } from "../../src/reduce";
import { verifyExport } from "../../src/verify";
import { unwrap } from "../store/helpers";

// Plan mode; a read_file call is recorded, then a tools_changed removes read_file. The policy
// decides the call by the spec it was made under (read_only: allowed in plan mode), as Python's
// authorize does, never by the latest set (where read_file is gone).
const LOG = join(
  import.meta.dir,
  "../../../../../spec/conformance/cases/recover-removed-tool-call-not-executed/log.threads-ts.jsonl",
);

test("authorize decides a call by its call-time spec", () => {
  const log = unwrap(verifyExport(readFileSync(LOG)));
  const call = knownEvents(log).find((e) => e.type === "tool_call");
  if (call?.type !== "tool_call") throw new Error("the case has a tool_call");
  const spec = callSpec(log.fold, call);
  if (spec === undefined)
    throw new Error("read_file was in the set at the call");
  expect(toolSpec(log.fold, "read_file")).toBeUndefined();
  expect(authorize(call, log.fold, spec).decision).toBe("allow");
});
