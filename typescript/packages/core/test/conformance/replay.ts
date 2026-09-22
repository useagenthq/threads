import type { KnownEvent } from "../../src/log";
import type { EventDraft } from "../../src/store";

/** A recorded event as a draft to append again: the envelope is the new writer's. */
export function redraft(e: KnownEvent): EventDraft {
  const {
    seq: _seq,
    event_id: _id,
    thread_id: _thread,
    branch_id: _branch,
    epoch: _epoch,
    time: _time,
    prev_hash: _prev,
    ...draft
  } = e;
  return draft;
}
