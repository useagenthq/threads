import type { Model } from "../model/protocol";
import type { Sandbox } from "../sandbox/protocol";
import { ConfigError } from "./errors";

// Setup's check of declared recovery lookups (spec/api.json Model.lookup, Sandbox.lookup,
// Sandbox.lookupSnapshot): an adapter that declares a lookup capability implements the method,
// so recovery never meets a declared lookup it can't ask. Run by check() and the first run.

/** Throws ConfigError capability_missing, naming the adapter and the missing method. */
export function checkLookups(
  models: readonly Model[],
  sandbox: Sandbox | undefined,
): void {
  for (const model of models) {
    const declared = model.info.lookup;
    if (declared !== "none" && model.lookup === undefined) {
      const { provider, name } = model.info.model;
      throw new ConfigError(
        "capability_missing",
        `model ${provider}/${name} declares lookup "${declared}" but has no lookup method: implement lookup, or declare lookup: "none"`,
      );
    }
  }
  if (sandbox === undefined) return;
  const { provider, lookup } = sandbox.info;
  if (lookup.create !== "none" && sandbox.lookup === undefined)
    throw missing(provider, "create", lookup.create, "lookup");
  if (lookup.snapshot !== "none" && sandbox.lookupSnapshot === undefined)
    throw missing(provider, "snapshot", lookup.snapshot, "lookupSnapshot");
}

function missing(
  provider: string,
  operation: string,
  declared: string,
  method: string,
): ConfigError {
  return new ConfigError(
    "capability_missing",
    `sandbox ${provider} declares lookup.${operation} "${declared}" but has no ${method} method: implement ${method}, or declare lookup.${operation}: "none"`,
  );
}
