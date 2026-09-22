import { z } from "zod";
import {
  Actor,
  canonicalize,
  JsonObject,
  PosInt,
  parseStrictJson,
} from "../log";
import { envelopeShape } from "../log/envelope";

/** The envelope fields the chain checks read before a line's type and data are known. */
export type ChainFields = {
  readonly seq: number;
  readonly prev_hash: string;
  readonly branch_id: string;
  readonly type: string;
  readonly data: { readonly [key: string]: unknown };
};

// Any event's envelope, whatever its type and data (conformance README, reduce step 1).
const envelope = z.strictObject({
  ...envelopeShape,
  type: z.string(),
  type_version: PosInt,
  critical: z.boolean(),
  actor: Actor,
  data: JsonObject,
});

/**
 * The envelope of an admissible event line whose type or data failed to parse, so that
 * seq, prev_hash and fork links are checked before the critical rule and the data schema.
 */
export function readEnvelope(text: string): ChainFields | undefined {
  const json = parseStrictJson(text);
  if (!json.ok) return undefined;
  const canonical = canonicalize(json.value);
  if (!canonical.ok || canonical.value !== text) return undefined;
  const parsed = envelope.safeParse(json.value);
  return parsed.success ? parsed.data : undefined;
}
