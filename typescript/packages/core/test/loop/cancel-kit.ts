import { scriptedModel } from "../../src";
import type { KnownEvent } from "../../src/log";
import type { Model } from "../../src/model";
import { markTestKit } from "../../src/model/guard";
import type { EventDraft, Writer } from "../../src/store";
import { unwrap } from "../store/helpers";

// A cancel that lands during a model attempt: the barrier, and a model that asks for it at a
// chosen send (and, with `leak`, then refuses its response as a leak, C5).

const encoder = new TextEncoder();

export const cancel: EventDraft = {
  type: "cancel_requested",
  type_version: 1,
  critical: true,
  actor: {
    kind: "user",
    principal: { issuer: "api", tenant: "acme", subject: "operator" },
  },
  data: { scope: "turn" },
};

/** The event types from the first `type` on. */
export const after = (
  log: readonly KnownEvent[],
  type: KnownEvent["type"],
): readonly string[] =>
  log.slice(log.findIndex((e) => e.type === type)).map((e) => e.type);

/**
 * Plays `responses` in order; send number `at` first appends the cancel through `writer`, and
 * with `leak` stores provider material holding that registered value.
 */
export function cancelAt(
  responses: readonly unknown[],
  at: number,
  writer: () => Writer,
  leak?: string,
): Model {
  const inner = scriptedModel({ responses: [...responses] });
  let sends = 0;
  const model: Model = {
    info: inner.info,
    send: async function* (request, context, options) {
      sends += 1;
      if (sends === at) {
        unwrap(await writer().append([cancel]));
        if (leak !== undefined)
          await context.put(
            encoder.encode(JSON.stringify({ encrypted: leak })),
            "application/json",
          );
      }
      yield* inner.send(request, context, options);
    },
  };
  markTestKit(model);
  return model;
}
