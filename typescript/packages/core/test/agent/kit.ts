import type { Model, ThreadRef } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { KnownEvent } from "../../src/log";
import { type ScriptedModel, scriptedModel } from "../../src/model";
import { markTestKit } from "../../src/model/guard";
import { knownEvents } from "../../src/reduce";
import { refReader, verifyRequests } from "../../src/render";
import { unwrap } from "../store/helpers";

// Shared by the agent tests: a thread's verified log, and a second scripted model.

/** The branch's events, with every recorded request re-verified (C7 and request_ref). */
export async function logOf(thread: ThreadRef): Promise<readonly KnownEvent[]> {
  const { log, artifacts } = await openStore(thread.store);
  const events = knownEvents(unwrap(log.read(thread.branch)));
  unwrap(verifyRequests(events, refReader(artifacts)));
  return events;
}

/**
 * A scripted model under another name, so policy.models lists two models. `inner` is the
 * script it plays, to read what reached it.
 */
export function scriptedSmall(responses: readonly unknown[]): Model & {
  readonly inner: ScriptedModel;
} {
  const inner = scriptedModel({ responses });
  const ref = { provider: "scripted", name: "scripted-small" };
  const small = {
    ...inner,
    info: {
      ...inner.info,
      model: ref,
      limits: { ...inner.info.limits, ...ref },
    },
    inner,
  };
  markTestKit(small);
  return small;
}
