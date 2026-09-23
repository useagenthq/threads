import { describe, expect, test } from "bun:test";
import type { LogStore, Writer } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { code, fixture, ROOT, started, THREAD, unwrap } from "./helpers";

// The resource ledger: rows move only through named, fenced transitions, and
// a store sees only its own tenant's rows.

function owner(tenant?: string, db = openBunSqlite(":memory:")) {
  const f = fixture(tenant, db);
  unwrap(f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(f.store.acquire(ROOT, "holder-a"));
  unwrap(writer.append([started]));
  return { ...f, writer };
}

const states = (store: LogStore): readonly string[] =>
  unwrap(store.ledger.rows()).map((r) => r.state);

describe("resource ledger transitions", () => {
  test("pending → live → releasing → released, with the audit kept", () => {
    const { store, writer, clock } = owner();
    const ledger = store.ledger;
    const row = unwrap(ledger.begin(writer, "sandbox", "fake"));
    expect(row.state).toBe("pending");
    expect(row.operation_key).toMatch(/^[0-9a-f-]{36}$/);
    unwrap(ledger.live(writer, row.resource_id, "sbx_1", null));
    unwrap(ledger.releasing(writer, row.resource_id));
    clock.now += 5;
    const done = unwrap(ledger.released(writer, row.resource_id, "released"));
    expect([done.state, done.ref, done.release_outcome]).toEqual([
      "released",
      "sbx_1",
      "released",
    ]);
    expect(done.released_at).toBe(clock.now);
  });

  test("an illegal move is invalid_transition", () => {
    const { store, writer } = owner();
    const row = unwrap(store.ledger.begin(writer, "sandbox", "fake"));
    expect(code(store.ledger.releasing(writer, row.resource_id))).toBe(
      "invalid_transition",
    );
    expect(states(store)).toEqual(["pending"]);
  });

  test("a stale owner can neither create nor release", () => {
    const { store, writer, clock } = owner();
    const row = unwrap(store.ledger.begin(writer, "sandbox", "fake"));
    unwrap(store.ledger.live(writer, row.resource_id, "sbx_1", null));
    clock.now += 60_000; // the lease lapses and another holder takes the branch
    const next: Writer = unwrap(store.acquire(ROOT, "holder-b"));
    expect(code(store.ledger.begin(writer, "sandbox", "fake"))).toBe(
      "stale_epoch",
    );
    expect(code(store.ledger.releasing(writer, row.resource_id))).toBe(
      "writer_poisoned",
    );
    expect(states(store)).toEqual(["live"]);
    unwrap(store.ledger.releasing(next, row.resource_id));
  });

  test("cleanup touches only releasing or failed rows, and only while no one holds the owner", () => {
    const { store, writer, clock } = owner();
    const ledger = store.ledger;
    const row = unwrap(ledger.begin(writer, "sandbox", "fake"));
    unwrap(ledger.live(writer, row.resource_id, "sbx_1", null));
    unwrap(ledger.releasing(writer, row.resource_id));
    expect(unwrap(ledger.collectable()).map((r) => r.resource_id)).toEqual([]);
    clock.now += 60_000;
    expect(unwrap(ledger.collectable()).map((r) => r.resource_id)).toEqual([
      row.resource_id,
    ]);
    unwrap(ledger.collected(row.resource_id, "release_failed", "boom"));
    unwrap(ledger.collected(row.resource_id, "released", "released"));
    expect(states(store)).toEqual(["released"]);
    expect(code(ledger.collected(row.resource_id, "released", "x"))).toBe(
      "invalid_transition",
    );
  });

  test("an expired live snapshot row is collectable; a live sandbox row is not", () => {
    const { store, writer, clock } = owner();
    const ledger = store.ledger;
    const snap = unwrap(ledger.begin(writer, "snapshot", "fake"));
    unwrap(ledger.live(writer, snap.resource_id, "snap_1", clock.now + 10));
    const box = unwrap(ledger.begin(writer, "sandbox", "fake"));
    unwrap(ledger.live(writer, box.resource_id, "sbx_1", null));
    clock.now += 60_000;
    expect(unwrap(ledger.collectable()).map((r) => r.ref)).toEqual(["snap_1"]);
    expect(code(ledger.collected(box.resource_id, "released", "x"))).toBe(
      "invalid_transition",
    );
  });
});

describe("the ledger is tenant-scoped", () => {
  test("another tenant's resource id can be neither read nor moved", () => {
    const db = openBunSqlite(":memory:");
    const acme = owner("acme", db);
    const row = unwrap(acme.store.ledger.begin(acme.writer, "sandbox", "fake"));
    unwrap(acme.store.ledger.live(acme.writer, row.resource_id, "sbx_1", null));
    unwrap(acme.store.ledger.releasing(acme.writer, row.resource_id));
    acme.clock.now += 60_000;

    const other = fixture("globex", db);
    expect(unwrap(other.store.ledger.rows())).toEqual([]);
    other.clock.now = acme.clock.now;
    expect(unwrap(other.store.ledger.collectable())).toEqual([]);
    expect(
      code(other.store.ledger.collected(row.resource_id, "released", "x")),
    ).toBe("not_found");
    expect(states(acme.store)).toEqual(["releasing"]);
  });
});
