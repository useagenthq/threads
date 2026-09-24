import type { EventOf, Fold } from "../fold/state";
import { sha256Hex } from "../hash";
import { canonicalize, JsonValue } from "../log";
import { conforms } from "./json-schema";
import { checkToolSet } from "./tool-set";
import { invalid, type Violation } from "./violation";

// Rules 17, 20 and 27.

/** Rule 17: tools_hash is the SHA-256 of the RFC 8785 bytes of tools, and the set stays safe. */
export function checkToolsChanged(
  fold: Fold,
  e: EventOf<"tools_changed">,
): Violation {
  // Parsed optional fields are typed `| undefined`; JsonValue gives back the plain JSON type.
  const tools = JsonValue.safeParse(e.data.tools);
  const text = tools.success ? canonicalize(tools.data) : undefined;
  return text?.ok === true && sha256Hex(text.value) === e.data.tools_hash
    ? checkToolSet(fold, e)
    : invalid("tools_hash is not the hash of tools");
}

/** Rule 20: the pinned output schema, and an accepted value that satisfies it. */
export function checkOutput(
  fold: Fold,
  e: EventOf<"output_validated">,
): Violation {
  const output = fold.policy?.output;
  if (output === undefined || output.schema_sha256 !== e.data.schema_sha256)
    return invalid("output_validated names a schema other than policy.output");
  if (e.data.outcome === "rejected") return undefined;
  return conforms(output.schema, e.data.value)
    ? undefined
    : invalid("the accepted value fails policy.output.schema");
}

/** Rule 27: from is the current mode; bypass needs allow_bypass. */
export function checkModeChanged(
  fold: Fold,
  e: EventOf<"mode_changed">,
): Violation {
  if (e.data.from !== fold.mode)
    return invalid(
      `mode_changed from ${e.data.from}, but the mode is ${fold.mode}`,
    );
  return e.data.to === "bypass" &&
    fold.policy?.permissions?.allow_bypass !== true
    ? invalid("mode_changed to bypass without policy.permissions.allow_bypass")
    : undefined;
}
