import { expect } from "bun:test";
import { BranchId, ThreadId } from "../../core/src/log";
import { ok } from "../../core/src/result";
import { LogStore } from "../../core/src/store";
import { uuidv7 } from "../../core/src/store/encode";
import { pgArtifacts } from "../src/artifacts";
import { openPg } from "../src/driver";
import { freshSchema, pgTest } from "./kit";
import { started, T0, unwrap, userInput } from "./store-kit";

// A team op puts and reads artifacts (a mail body over the inline cap, a reply's text) inside its
// decided append. On Postgres an artifact runs on a connection of its own: with more such appends
// at once than the pool has connections, each holds one and waits for another. Artifacts have a
// pool of their own, so none waits on the others.

const utf8 = new TextEncoder();

pgTest(
  "more concurrent appends that put and read artifacts than pool connections all commit",
  async () => {
    const { url, drop } = await freshSchema();
    const db = openPg(url, {}, { max: 2 });
    try {
      const artifacts = pgArtifacts(db, () => T0);
      const store = unwrap(await LogStore.open(db, () => T0, artifacts));
      const writers = await Promise.all(
        Array.from({ length: 8 }, async (_, i) => {
          const thread = ThreadId.parse(uuidv7(T0 + i));
          const branch = BranchId.parse(uuidv7(T0 + i));
          unwrap(await store.createBranch(thread, branch));
          const writer = unwrap(await store.acquire(branch, `w${i}`));
          unwrap(await writer.append([started]));
          return writer;
        }),
      );
      const done = await Promise.all(
        writers.map((w, i) =>
          w.appendDecided(async () => {
            const sha = await artifacts.put(utf8.encode(`body ${i}`));
            const read = unwrap(await artifacts.get(sha));
            return ok([userInput(new TextDecoder().decode(read))]);
          }),
        ),
      );
      expect(done.map((d) => ("kind" in d ? "refused" : d.ok))).toEqual(
        Array.from({ length: 8 }, () => true),
      );
    } finally {
      await db.close();
      await drop();
    }
  },
  30_000,
);
