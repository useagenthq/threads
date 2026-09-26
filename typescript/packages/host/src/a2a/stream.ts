import {
  type A2aFault,
  fault,
  isSettled,
  type SseFrame,
  type StreamResponse,
  type Task,
} from "@threads/a2a/protocol";
import type { Principal } from "@threads/core/host";
import type { HostContext } from "../context";
import { type Located, slicesOf } from "./read";
import { artifactsOf, type Slice, taskOf } from "./state";

// A task's SSE frames: first a task snapshot, then a statusUpdate per state change, then an
// artifactUpdate for the final output. Every frame is a function of the committed events alone —
// there are no live token deltas here — so a resume at any frame boundary replays exactly the
// frames after it, and a reconnecting client never sees one twice.

const POLL_MS = 50;

/** A frame's position: the seq it was read at, and which frame of that seq it is. */
export type Cursor = { readonly seq: number; readonly k: number };

/** `<seq>:<k>`, or the InvalidParamsError a malformed Last-Event-ID earns. */
export function parseCursor(raw: string | null): Cursor | undefined | A2aFault {
  if (raw === null || raw === "") return undefined;
  const found = /^(\d+):(\d+)$/.exec(raw);
  const seq = Number(found?.[1]);
  const k = Number(found?.[2]);
  if (found === null || !Number.isSafeInteger(seq) || !Number.isSafeInteger(k))
    return fault("InvalidParamsError", `Last-Event-ID ${raw} is not <seq>:<k>`);
  return { seq, k };
}

function after(cursor: Cursor | undefined, at: Cursor): boolean {
  if (cursor === undefined) return true;
  return at.seq > cursor.seq || (at.seq === cursor.seq && at.k > cursor.k);
}

export type Frame = { readonly at: Cursor; readonly item: StreamResponse };

/**
 * The frames the run's states have produced, in order: a task snapshot, then one statusUpdate per
 * state change, then the artifactUpdate of a completed run. Computed from the prefix slices alone,
 * so the list for a given log is always the same one and only ever grows at its end.
 */
export function framesOf(slices: readonly Slice[]): readonly Frame[] {
  const frames: Frame[] = [];
  let shown: string | undefined;
  for (const slice of slices) {
    const task = taskOf(slice);
    const seq = slice.own.at(-1)?.seq ?? 0;
    if (shown === undefined) {
      frames.push({ at: { seq, k: 0 }, item: { task } });
      shown = task.status.state;
      continue;
    }
    if (task.status.state === shown) continue;
    shown = task.status.state;
    let k = 0;
    frames.push({
      at: { seq, k },
      item: {
        statusUpdate: {
          taskId: task.id,
          contextId: slice.contextId,
          status: task.status,
        },
      },
    });
    for (const artifact of artifactsOf(slice) ?? [])
      frames.push({
        at: { seq, k: ++k },
        item: {
          artifactUpdate: {
            taskId: task.id,
            contextId: slice.contextId,
            artifact,
            lastChunk: true,
          },
        },
      });
  }
  return frames;
}

/** Whether the stream is over: the task can go no further on its own. */
function closed(task: Task): boolean {
  return isSettled(task.status.state);
}

/**
 * The frames after `cursor`, following the log until the task settles. Only committed events are
 * streamed, so a follower that joins late and one that was there from the start see the same
 * frames in the same order.
 */
export async function* follow(
  ctx: HostContext,
  principal: Principal,
  at: Located,
  cursor: Cursor | undefined,
  item: (item: StreamResponse) => string,
): AsyncGenerator<SseFrame> {
  let seen = cursor;
  for (;;) {
    const slices = await slicesOf(ctx, principal.tenant, at);
    const last = slices?.at(-1);
    if (slices === undefined || last === undefined) return;
    for (const frame of framesOf(slices))
      if (after(seen, frame.at)) {
        seen = frame.at;
        yield { id: `${frame.at.seq}:${frame.at.k}`, data: item(frame.item) };
      }
    if (closed(taskOf(last))) return;
    const { promise, resolve } = Promise.withResolvers<void>();
    setTimeout(resolve, POLL_MS);
    await promise;
  }
}
