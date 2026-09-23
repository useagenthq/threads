import type { KnownEvent } from "../log";
import { knownEvents } from "../reduce";
import { captureSnapshot, type Sandbox, snapshotEvent } from "../sandbox";
import type { ResourceLedger, Writer } from "../store";
import type { SessionGetter } from "../tools";

// The end-of-turn snapshot policy: a turn that ran a tool whose class
// is not read_only ends with a capture. It runs after the loop is idle, so no append or dispatch
// happens between the capture and its event (the writer barrier, ), and only a
// capture captureSnapshot verified is recorded. A failed capture records nothing.

/** The last turn completed and began an effect: only non-read_only calls write effect_begin. */
function ranEffects(events: readonly KnownEvent[]): boolean {
  if (events.at(-1)?.type !== "turn_completed") return false;
  const start = events.findLastIndex((e) => e.type === "user_input");
  return events.slice(start + 1).some((e) => e.type === "effect_begin");
}

export async function snapshotTurn(
  sandbox: Sandbox | undefined,
  session: SessionGetter,
  ledger: ResourceLedger,
  writer: Writer,
  /** The corpus revision this branch searches as of, when it has knowledge. */
  knowledgeRevision?: number,
): Promise<void> {
  if (sandbox === undefined || !ranEffects(knownEvents(writer.chain))) return;
  const live = await session();
  if (!live.ok) return;
  const captured = await captureSnapshot(ledger, writer, sandbox, live.value);
  // ponytail: a refused capture only costs this turn its fork point; it is not reported yet.
  if (captured.ok)
    writer.append([snapshotEvent(captured.value, knowledgeRevision)]);
}
