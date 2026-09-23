import { ok } from "../../src/result";
import type { SandboxContext } from "../../src/sandbox";
import { ROOT } from "../store/helpers";

/** A context that always passes its fence: for driving the fake directly in tests. */
export const CTX: SandboxContext = {
  authority: { kind: "owner", branch_id: ROOT, epoch: 1 },
  fence: async () => ok(undefined),
};

/** What a caller passes to exec, as objects it could still change. */
export type MutableCall = {
  command: string[];
  options: {
    processKey: string;
    cwd: string;
    env: Record<string, string>;
    stdin?: Uint8Array;
    timeoutMs?: number;
  };
};

/**
 * A context whose every fence changes the caller's exec inputs: an adapter that re-reads
 * them after an await runs something other than what it checked.
 */
export function changingInputs(call: MutableCall): SandboxContext {
  return {
    ...CTX,
    fence: async () => {
      call.command.splice(0, call.command.length, "printenv");
      call.options.processKey = `${call.options.processKey}-changed`;
      call.options.cwd = "not/absolute";
      // A deadline re-read after the fence would never fire within the test's time.
      call.options.timeoutMs = 3_600_000;
      call.options.env["BAD NAME"] = "x";
      call.options.env["CHANGED"] = "1";
      return ok(undefined);
    },
  };
}
