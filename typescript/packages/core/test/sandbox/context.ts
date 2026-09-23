import { ok } from "../../src/result";
import type { SandboxContext } from "../../src/sandbox";
import { ROOT } from "../store/helpers";

/** A context that always passes its fence: for driving the fake directly in tests. */
export const CTX: SandboxContext = {
  authority: { kind: "owner", branch_id: ROOT, epoch: 1 },
  fence: async () => ok(undefined),
};
