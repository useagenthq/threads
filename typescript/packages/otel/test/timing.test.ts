import { afterEach, describe, expect, test } from "bun:test";
import { deleteThread, ThreadId } from "@threads/core/host";
import { accepted, type Collector, collector } from "./collector";
import { goldens } from "./goldens";
import { cursors, exporter } from "./kit";
import { cut, grow, type Harness, harness, importGolden } from "./store";

// When a sync runs never changes what is sent: a sync after every append and one sync at the end
// send the same spans, leave the same cursors, and report the same possibly_lost spans for a
// thread deleted in the middle. Every golden, through a real store.

const all = goldens();
const doomed = all.find((g) => g.name === "otel-subagent-parent-missing");
const DOOMED_THREAD = ThreadId.parse("0192a000-0000-7000-8000-0000000000c1");

let c: Collector;
afterEach(async () => {
  await c.stop();
});

type Outcome = {
  readonly spans: readonly string[];
  readonly cursors: Readonly<Record<string, number>>;
};

async function run(
  g: (typeof all)[number],
  everyAppend: boolean,
): Promise<Outcome> {
  c = collector();
  const h: Harness = await harness();
  if (doomed !== undefined) await importGolden(h, doomed);
  await importGolden(h, g);
  const last = g.syncs.at(-1);
  const branch = last?.branch_id ?? "";
  const rows = await cut(
    h,
    branch,
    g.syncs.find((s) => s.branch_id === branch)?.cursor_before ?? 0,
  );
  const e = exporter(h.store, c.url);
  const sync = async (): Promise<void> => {
    const sent = await e.sync();
    if (!sent.ok) throw new Error(sent.error.message);
  };
  await sync();
  for (const [i, row] of rows.entries()) {
    await grow(h, branch, row);
    if (i === Math.floor(rows.length / 2)) {
      const gone = await deleteThread(
        h.db,
        "local",
        DOOMED_THREAD,
        h.clock.now,
      );
      if (!gone.ok) throw new Error(gone.error.message);
    }
    if (everyAppend) await sync();
  }
  await sync();
  return {
    spans: accepted(c)
      .map((s) => JSON.stringify(s))
      .toSorted(),
    cursors: await cursors(h.store),
  };
}

describe("a sync after every append sends what one sync at the end sends", () => {
  for (const g of all.filter(
    (x) => !x.branches.includes(doomed?.branches[0] ?? ""),
  ))
    test(g.name, async () => {
      const once = await run(g, false);
      const ticked = await run(g, true);
      expect(ticked.spans).toEqual(once.spans);
      expect(ticked.cursors).toEqual(once.cursors);
      expect(
        once.spans.some((s) => s.includes("threads.export.possibly_lost")),
      ).toBe(true);
    });
});
