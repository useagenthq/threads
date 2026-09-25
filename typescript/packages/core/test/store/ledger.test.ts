import { describe, expect, test } from "bun:test";
import type { LogStore, Writer } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { code, fixture, ROOT, started, THREAD, unwrap } from "./helpers";

// The resource ledger: rows move only through named, fenced transitions, and
// a store sees only its own tenant's rows.

async function owner(tenant?: string, db = openBunSqlite(":memory:")) {
  const f = await fixture(tenant, db);
  unwrap(await f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(await f.store.acquire(ROOT, "holder-a"));
  unwrap(await writer.append([started]));
  return { ...f, writer };
}

const states = async (store: LogStore): Promise<readonly string[]> =>
  unwrap(await store.ledger.rows()).map((r) => r.state);

describe("resource ledger transitions", () => {
  test("pending → live → releasing → released, with the audit kept", async () => {
    const { store, writer, clock } = await owner();
    const ledger = store.ledger;
    const row = unwrap(await ledger.begin(writer, "sandbox", "fake"));
    expect(row.state).toBe("pending");
    expect(row.operation_key).toMatch(/^[0-9a-f-]{36}$/);
    unwrap(await ledger.live(writer, row.resource_id, "sbx_1", null));
    unwrap(await ledger.releasing(writer, row.resource_id));
    clock.now += 5;
    const done = unwrap(
      await ledger.released(writer, row.resource_id, "released"),
    );
    expect([done.state, done.ref, done.release_outcome]).toEqual([
      "released",
      "sbx_1",
      "released",
    ]);
    expect(done.released_at).toBe(clock.now);
  });

  test("an illegal move is invalid_transition", async () => {
    const { store, writer } = await owner();
    const row = unwrap(await store.ledger.begin(writer, "sandbox", "fake"));
    expect(code(await store.ledger.releasing(writer, row.resource_id))).toBe(
      "invalid_transition",
    );
    expect(await states(store)).toEqual(["pending"]);
  });

  test("a stale owner can neither create nor release", async () => {
    const { store, writer, clock } = await owner();
    const row = unwrap(await store.ledger.begin(writer, "sandbox", "fake"));
    unwrap(await store.ledger.live(writer, row.resource_id, "sbx_1", null));
    clock.now += 60_000; // the lease lapses and another holder takes the branch
    const next: Writer = unwrap(await store.acquire(ROOT, "holder-b"));
    expect(code(await store.ledger.begin(writer, "sandbox", "fake"))).toBe(
      "stale_epoch",
    );
    expect(code(await store.ledger.releasing(writer, row.resource_id))).toBe(
      "writer_poisoned",
    );
    expect(await states(store)).toEqual(["live"]);
    unwrap(await store.ledger.releasing(next, row.resource_id));
  });

  test("cleanup touches only releasing or failed rows, and only while no one holds the owner", async () => {
    const { store, writer, clock } = await owner();
    const ledger = store.ledger;
    const row = unwrap(await ledger.begin(writer, "sandbox", "fake"));
    unwrap(await ledger.live(writer, row.resource_id, "sbx_1", null));
    unwrap(await ledger.releasing(writer, row.resource_id));
    expect(
      unwrap(await ledger.collectable()).map((r) => r.resource_id),
    ).toEqual([]);
    clock.now += 60_000;
    expect(
      unwrap(await ledger.collectable()).map((r) => r.resource_id),
    ).toEqual([row.resource_id]);
    const claim = unwrap(await ledger.claim(row.resource_id));
    unwrap(
      await ledger.collected(row.resource_id, claim, "release_failed", "boom"),
    );
    unwrap(
      await ledger.collected(row.resource_id, claim, "released", "released"),
    );
    expect(await states(store)).toEqual(["released"]);
    expect(code(await ledger.claim(row.resource_id))).toBe(
      "invalid_transition",
    );
    expect(
      code(await ledger.collected(row.resource_id, claim, "released", "x")),
    ).toBe("stale_epoch");
  });

  test("a later cleanup claim supersedes an earlier one: only its holder may record", async () => {
    const { store, writer, clock } = await owner();
    const ledger = store.ledger;
    const row = unwrap(await ledger.begin(writer, "sandbox", "fake"));
    unwrap(await ledger.live(writer, row.resource_id, "sbx_1", null));
    unwrap(await ledger.releasing(writer, row.resource_id));
    clock.now += 60_000;
    const first = unwrap(await ledger.claim(row.resource_id));
    const second = unwrap(await ledger.claim(row.resource_id));
    expect(code(await ledger.claimed(row.resource_id, first))).toBe(
      "stale_epoch",
    );
    expect(
      code(await ledger.collected(row.resource_id, first, "released", "x")),
    ).toBe("stale_epoch");
    unwrap(
      await ledger.collected(row.resource_id, second, "released", "released"),
    );
  });

  test("an expired live snapshot row is collectable; a live sandbox row is not", async () => {
    const { store, writer, clock } = await owner();
    const ledger = store.ledger;
    const snap = unwrap(await ledger.begin(writer, "snapshot", "fake"));
    unwrap(
      await ledger.live(writer, snap.resource_id, "snap_1", clock.now + 10),
    );
    const box = unwrap(await ledger.begin(writer, "sandbox", "fake"));
    unwrap(await ledger.live(writer, box.resource_id, "sbx_1", null));
    clock.now += 60_000;
    expect(unwrap(await ledger.collectable()).map((r) => r.ref)).toEqual([
      "snap_1",
    ]);
    expect(code(await ledger.claim(box.resource_id))).toBe(
      "invalid_transition",
    );
  });
});

describe("the ledger is tenant-scoped", () => {
  test("another tenant's resource id can be neither read nor moved", async () => {
    const db = openBunSqlite(":memory:");
    const acme = await owner("acme", db);
    const row = unwrap(
      await acme.store.ledger.begin(acme.writer, "sandbox", "fake"),
    );
    unwrap(
      await acme.store.ledger.live(acme.writer, row.resource_id, "sbx_1", null),
    );
    unwrap(await acme.store.ledger.releasing(acme.writer, row.resource_id));
    acme.clock.now += 60_000;

    const other = await fixture("globex", db);
    expect(unwrap(await other.store.ledger.rows())).toEqual([]);
    other.clock.now = acme.clock.now;
    expect(unwrap(await other.store.ledger.collectable())).toEqual([]);
    expect(code(await other.store.ledger.claim(row.resource_id))).toBe(
      "not_found",
    );
    const claim = unwrap(await acme.store.ledger.claim(row.resource_id));
    expect(
      code(
        await other.store.ledger.collected(
          row.resource_id,
          claim,
          "released",
          "x",
        ),
      ),
    ).toBe("not_found");
    expect(await states(acme.store)).toEqual(["releasing"]);
  });
});
