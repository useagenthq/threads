import { z } from "zod";
import { InvalidCursorError } from "../../src/agent/errors";
import { teamEvents } from "../../src/agent/team/feed";
import type { TeamCursor } from "../../src/agent/team/handle-types";
import type { TeamId } from "../../src/log";
import type { LogStore } from "../../src/store";

// The `feed` projection of a Teams Phase 2 team case (spec/conformance/README.md): one answer per
// `input.feed` read of the rebuilt feed, each item named by (branch_id, seq) rather than by the
// whole event. Reference: spec/tools/fixtures/host_feed.py.

/** One `input.feed` read: from the start, or after a cursor. */
export type FeedRead = { readonly after?: TeamCursor | undefined };

export const FeedReads: z.ZodType<readonly FeedRead[]> = z.array(
  z.strictObject({
    after: z.strictObject({ epoch: z.int(), offset: z.int() }).optional(),
  }),
);

type Item = {
  readonly kind: string;
  readonly cursor: TeamCursor;
  readonly source?: unknown;
  readonly branch_id?: string;
  readonly seq?: number;
};
type Answer = { readonly items: readonly Item[] } | { readonly error: string };

/** One read: every item after its cursor, or invalid_cursor. */
async function read(
  store: LogStore,
  team: TeamId,
  after: TeamCursor | undefined,
): Promise<Answer> {
  const items: Item[] = [];
  try {
    for await (const item of teamEvents(
      store,
      team,
      after === undefined ? {} : { after },
    ))
      items.push(
        item.kind === "epoch_restarted"
          ? { kind: item.kind, cursor: item.cursor }
          : {
              kind: item.kind,
              cursor: item.cursor,
              source: item.source,
              branch_id: item.event.branch_id,
              seq: item.event.seq,
            },
      );
  } catch (error) {
    if (error instanceof InvalidCursorError) return { error: "invalid_cursor" };
    throw error;
  }
  return { items };
}

/** Every `input.feed` read, in order. */
export async function feedAnswers(
  store: LogStore,
  team: TeamId,
  reads: readonly FeedRead[],
): Promise<readonly Answer[]> {
  const out: Answer[] = [];
  for (const one of reads) out.push(await read(store, team, one.after));
  return out;
}
