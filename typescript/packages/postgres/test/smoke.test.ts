import { expect } from "bun:test";
import { BranchId, ThreadId } from "@threads/core/host";
import { LogStore } from "../../core/src/store";
import { pgArtifacts } from "../src/artifacts";
import { openPg } from "../src/driver";
import { freshSchema, pgTest } from "./kit";

pgTest("a branch appends, reads back and exports on Postgres", async () => {
  const { url, drop } = await freshSchema();
  const db = openPg(url);
  try {
    const store = await LogStore.open(db, () => 1_000, pgArtifacts(db));
    if (!store.ok) throw new Error(store.error.message);
    const t = ThreadId.parse("00000000-0000-4000-8000-000000000001");
    const b = BranchId.parse("00000000-0000-4000-8000-000000000002");
    expect((await store.value.createBranch(t, b)).ok).toBe(true);
    const w = await store.value.acquire(b, "h");
    if (!w.ok) throw new Error(w.error.message);
    const read = await store.value.read(b);
    expect(read.ok).toBe(true);
    const bytes = await store.value.exportBranch(b);
    expect(bytes.ok).toBe(true);
    await w.value.release();
  } finally {
    await db.close();
    await drop();
  }
});
