import {
  type ArtifactRef,
  canonicalize,
  type EventDraft,
  JsonValue,
  type ToolContext,
} from "threadsai/adapter";
import type { Task } from "../protocol";

// The remote_task_state events an exchange observed. Each is what we SAW, never what we decided,
// which is why they are not critical: a reader that skips them reduces the log to the same state.
// Appended after the call's effect_commit, because a partner's task state is only ever observed
// for a call we hold a receipt for (semantic rule 57).

/**
 * One draft per state change, deduplicated by state **and** content hash: a repeat of the same
 * state with the same bytes is not appended twice, within this exchange or against what earlier
 * attempts of the same call already recorded.
 */
export async function stateDrafts(
  ctx: ToolContext,
  callId: string,
  seen: readonly Task[],
  already: ReadonlySet<string>,
): Promise<readonly EventDraft[]> {
  const drafts: EventDraft[] = [];
  const kept = new Set(already);
  for (const task of seen) {
    const status = await stored(ctx, task.status);
    const key = `${task.status.state}:${status?.sha256 ?? ""}`;
    if (kept.has(key)) continue;
    kept.add(key);
    const artifacts =
      task.artifacts === undefined || task.artifacts.length === 0
        ? undefined
        : await stored(ctx, task.artifacts);
    drafts.push({
      type: "remote_task_state",
      type_version: 1,
      critical: false,
      actor: { kind: "host" },
      data: {
        call_id: callId,
        task_id: task.id,
        state: task.status.state,
        ...(status === undefined ? {} : { status_ref: status }),
        ...(artifacts === undefined ? {} : { artifacts_ref: artifacts }),
      },
    });
  }
  return drafts;
}

/** The observation's bytes as canonical JSON, so the same observation hashes the same everywhere. */
async function stored(
  ctx: ToolContext,
  value: unknown,
): Promise<ArtifactRef | undefined> {
  const json = JsonValue.safeParse(value);
  if (!json.success) return undefined;
  const text = canonicalize(json.data);
  return text.ok ? ctx.store(text.value, "application/json") : undefined;
}
